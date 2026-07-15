import appdaemon.plugins.hass.hassapi as hass # type: ignore
import datetime
import re
import time

import lighting_actions
import room_state_darkness

class FamilyRoomLights(hass.Hass):
    """
    Family room lights with presence detection, door awareness, sleep mode and enhanced illuminance calculation.
    Uses centralized darkness calculator with adaptive thresholds for clear vs cloudy conditions.

    UX - daylight vs lights (hybrid):
    - Auto-off / island / sleep decisions use **committed** ``darkness_calculator`` output only:
      ``sensor.darkness_*`` state (``dark`` / ``bright``) and/or the ``(Dark|Bright)`` part of
      ``sensor.room_state_*``.
    - Auto-on uses the same committed dark/bright state as auto-off (``darkness_calculator``).
    - Committed-state listeners remain state-only (room_state state + ``sensor.darkness_*`` state),
      not ``pending_target`` attributes.

    UX - presence (traceable in UI):
    - Optional ``family_presence_room_keys`` selects which ``raw_pir_sensors`` count as "family zone" (OR).
      **No extra delay in this app** - occupancy matches those HA entities so dashboards and troubleshooting
      stay aligned; tune hold/decay only in the sensor / AOD / device layer.

    Presence source note:
    This app expects boolean on/off "presence" entities. In this setup we point those at
    Area Occupancy Detection (AOD) `*_occupancy_status` entities (instead of raw PIR/mmWave),
    so the lighting decisions benefit from AOD's probability + decay behavior.
    """

    def initialize(self):
        try:
            # Load essential config
            self.light_map = self.args["light_map"]
            self._dishwasher_state_entity = self.args.get("dishwasher_state_entity")
            
            # Validate the 'all' group exists in light_map
            if "all" not in self.light_map:
                self.log("CRITICAL ERROR: Missing 'all' group in light_map", level="ERROR")
                raise ValueError("Missing 'all' group in light_map")
                
            self.presence = self.args.get("raw_pir_sensors", {})
            self.raw_pir_sensors = self.presence
            self.raw_adjacent_pir_sensors = self.args.get("raw_adjacent_pir_sensors", {})

            _fp_keys = self.args.get("family_presence_room_keys")
            if isinstance(_fp_keys, list) and len(_fp_keys) > 0:
                self._family_presence_sensors = {
                    k: v for k, v in self.presence.items() if k in _fp_keys
                }
                if not self._family_presence_sensors:
                    self.log(
                        "family_presence_room_keys matched no sensors - using all raw_pir_sensors",
                        level="WARNING",
                    )
                    self._family_presence_sensors = dict(self.presence)
                else:
                    self.log(
                        f"Family zone PIRs (main rooms): {list(self._family_presence_sensors.keys())}",
                        level="INFO",
                    )
            else:
                self._family_presence_sensors = dict(self.presence)

            self.lux = self.args["illuminance_sensors"]
            
            # Centralized, human-readable room state published by darkness_calculator
            # Prefer a single combined helper; fallback to multiple per-zone helpers or a dark binary sensor
            self.room_state_text_entity = self.args.get("room_state_text_entity")
            self.room_state_text_entities = self.args.get("room_state_text_entities", []) or []
            self.room_dark_binary_entity = self.args.get("room_dark_binary_entity")
            
            # Sunrise/sunset offsets no longer used here (handled centrally)
            
            # No local darkness thresholds; centralized state is used
            

            
            # Other parameters
            # Remove local smoothing/lockout
            
            # Hysteresis state for darkness detection
            self._dark_flag = None
            self._last_darkness_state = None
            self._last_state_change_time = None
            
            # State tracking for smart logging
            self._last_adjacent_room_presence = {}
            self._last_mode_log = ""
            self._darkness_log_count = 0
            self._last_family_presence = False
            self._last_presence_room = None
            self._family_presence_log_count = 0
            self._family_presence_log_interval = int(self.args["family_presence_log_interval"])
            self._action_log_count = 0  # Add missing initialization
            # Tracks if sleep mode was activated during an ongoing family-room presence session
            self._sleep_activated_during_presence = False

            # Diagnostics sensor (thresholds + toggle / evaluation counters)
            self._diag_sensor = self.args.get("diagnostics_sensor_entity") or None
            if self._diag_sensor == "":
                self._diag_sensor = None
            self._diag_day = None
            self._diag_phys_on_today = 0
            self._diag_phys_off_today = 0
            self._diag_phys_on_session = 0
            self._diag_phys_off_session = 0
            self._diag_eval_today = 0
            self._diag_eval_session = 0
            self._diag_last_action = None
            self._diag_last_reason = None
            self._diag_last_details = ""

            # Debouncer for state change evaluations
            # Instead of rerun mechanism, use debouncing: schedule evaluation after a delay,
            # cancel previous if new state change occurs. This batches rapid state changes.
            self._evaluation_timer = None  # Timer for debounced evaluation
            # Monotonic token: pending run_in callbacks with an older token no-op (avoids cancel_timer/stale handles).
            self._eval_token = 0
            self._evaluation_debounce_delay = 0.3  # Seconds to wait before evaluating (allows state changes to settle)
            self._last_evaluation_time = 0.0  # Track last evaluation time for rate limiting
            self._min_evaluation_interval = 0.3  # Minimum seconds between actual evaluations (prevents CPU spikes)
            
            # Logging optimization parameters
            self._last_darkness_score = None
            self._last_darkness_values = None
            self._darkness_change_threshold = float(self.args.get("darkness_change_threshold", 0.1))
                
            self._last_presence_summary = None
            self._presence_summary_interval = int(self.args["presence_summary_interval"])
            
            # Logging configuration
            self.log_level = self.args["verbosity_level"]
            
            # Load door configs
            self.doors = self.args["door_sensors"]
            self.adjacent_presence = self.args.get("raw_adjacent_pir_sensors", {})
            self.adjacent_rooms = ["bedroom", "kristines_room", "claudias_room", "guest_bathroom"]
            
            # Specially handle rooftop door
            self.rooftop_door_sensor = self.args["rooftop_door_sensor"]
            if self.rooftop_door_sensor:
                self.log(f"Configured rooftop door sensor: {self.rooftop_door_sensor}", level="INFO")
            # Global mechanism: per-zone manual override pauses automatic control of that zone's lights
            self.manual_override_entity = self.args.get("manual_override_boolean")  # optional whole-app toggle
            self.manual_override_booleans = self.args.get("manual_override_booleans") or {}

            # Optional: ``sensor.darkness_<zone>`` - same confirmed dark/bright as labels (see darkness_calculator).
            self._darkness_confirmed_sensor = self.args.get("darkness_confirmed_sensor_entity") or None
            if self._darkness_confirmed_sensor:
                self.log(
                    f"Confirmed darkness sensor (daylight turn-off gate): {self._darkness_confirmed_sensor}",
                    level="INFO",
                )

            # Apartment entry door (template OR of Yale sensors) - arrival latch + guest-exit
            self.apartment_entry_door_sensor = self.args.get("apartment_entry_door_sensor")
            self.guest_exit_require_entry_door_closed = bool(
                self.args.get("guest_exit_require_entry_door_closed", False)
            )
            self.guest_exit_use_adjacent_doors_only = bool(
                self.args.get("guest_exit_use_adjacent_doors_only", True)
            )
            self.manual_bright_listen_groups = list(
                self.args.get("manual_bright_listen_groups") or ["living", "dining"]
            )
            self._manual_bright_echo_seconds = float(
                self.args.get("manual_bright_echo_suppress_seconds", 1.5)
            )
            self._door_arrival_latch = False
            self._latch_zone_persons_snapshot = None
            self._manual_bright_echo_until = {}
            self._manual_bright_watch = set()
            for grp in self.manual_bright_listen_groups:
                for lt in self.light_map.get(grp, []) or []:
                    self._manual_bright_watch.add(lt)
            
            # Load sleep mode booleans
            self.sleep_modes = self.args["sleep_mode_booleans"]
            if self.sleep_modes:
                self.log(f"Loaded {len(self.sleep_modes)} sleep mode booleans", level="INFO")
                # Register handlers for sleep mode changes
                for sleep_mode in self.sleep_modes:
                    try:
                        self.listen_state(self._on_sleep_mode_change, sleep_mode)
                        current_state = self.get_state(sleep_mode)
                        self.log(f"Sleep mode {sleep_mode} is currently {current_state}", level="INFO")
                    except Exception as e:
                        self.log(f"Error setting up sleep mode {sleep_mode}: {e}", level="ERROR")
            
            # Timing parameters
            self.initial_check_delay = int(self.args["initial_check_delay"])
            self.check_interval = int(self.args["check_interval"])
            
            # Illuminance: still read for minor hysteresis and freshness only
            self.illuminance_hysteresis = float(self.args.get("illuminance_hysteresis", 50))
            
            # Check current states instead of blindly turning off lights
            # Removed verbose initialization message for streamlined logging
            self.run_in(self._check_lights, self.initial_check_delay)  # Run light check after delay to allow all states to load
            
            # No periodic check needed - we have state listeners on all sensors (presence, doors, illuminance, etc.)
            # State changes will trigger evaluations via _schedule_evaluation()
            # This eliminates unnecessary CPU usage when room is empty and nothing is changing
            
            if self.raw_pir_sensors:
                for room, sensor in self.raw_pir_sensors.items():
                    try:
                        self.listen_state(self._on_raw_pir_off, sensor, old="on", new="off", room=room)
                        if room in self._family_presence_sensors:
                            self.listen_state(
                                self._on_raw_pir_on,
                                sensor,
                                old="off",
                                new="on",
                                room=room,
                            )
                        self.log(f"Registered PIR off handler for {room}: {sensor}", level="INFO")
                    except Exception as e:
                        self.log(f"Error registering PIR off handler for {room}: {e}", level="ERROR")

            # Register adjacent presence handlers
            if isinstance(self.adjacent_presence, dict):
                for room, sensor in self.adjacent_presence.items():
                    try:
                        self.listen_state(self._on_adjacent_presence_change, sensor)
                        self.log(f"Registered adjacent presence handler for {room}", level="INFO")
                    except Exception as e:
                        self.log(f"Error registering adjacent presence handler for {room}: {e}", level="ERROR")
            
            # Listen to centralized family room state helper(s)
            try:
                lighting_actions.register_room_state_push_listeners(
                    self,
                    self._on_room_state_push,
                    room_state_entity=self.room_state_text_entity,
                    darkness_sensor=self._darkness_confirmed_sensor,
                )
                if isinstance(self.room_state_text_entities, (list, tuple)):
                    for ent in self.room_state_text_entities:
                        try:
                            lighting_actions.register_room_state_push_listeners(
                                self, self._on_room_state_push, room_state_entity=ent
                            )
                            self.log(f"Listening to room state helper: {ent}", level="INFO")
                        except Exception as ie:
                            self.log(f"Failed to listen to room state helper {ent}: {ie}", level="ERROR")
                if self.room_dark_binary_entity:
                    try:
                        self.listen_state(self._on_room_state_push, self.room_dark_binary_entity)
                        self.log(f"Listening to room dark binary: {self.room_dark_binary_entity}", level="INFO")
                    except Exception as be:
                        self.log(f"Failed to listen to room dark binary {self.room_dark_binary_entity}: {be}", level="ERROR")
            except Exception as e:
                self.log(f"Failed to register room state listeners: {e}", level="ERROR")

            _override_entities = set()
            if getattr(self, "manual_override_entity", None):
                _override_entities.add(self.manual_override_entity)
            for _ent in (getattr(self, "manual_override_booleans", {}) or {}).values():
                if _ent:
                    _override_entities.add(_ent)
            for _ent in sorted(_override_entities):
                try:
                    self.listen_state(self._on_manual_override_change, _ent)
                    self.log(f"Registered manual override listener: {_ent}", level="INFO")
                except Exception as e:
                    self.log(f"Error registering manual override listener ({_ent}): {e}", level="ERROR")

            # Register hallway light enforcement while child sleep is active
            for hall_light in self.light_map.get("hallway", []):
                try:
                    self.listen_state(self._on_hallway_light_state_change, hall_light)
                    self.log(f"Registered hallway light enforcement for {hall_light}", level="INFO")
                except Exception as e:
                    self.log(f"Error registering hallway light enforcement for {hall_light}: {e}", level="ERROR")
            
            # Safely register door handlers
            if self.doors:
                self.log("Setting up door handlers", level="INFO")
            for room, sensor in self.doors.items():
                try:
                    # Log current door state (INFO only if open to reduce noise)
                    current_state = self.get_state(sensor)
                    if current_state == "on":
                        self.log(f"Door sensor {room} is open at startup: {sensor}", level="INFO")
                    else:
                        self.log(f"Door sensor {room}: {sensor} is {current_state}", level="DEBUG")
                    
                    # Register door open handler
                    self.listen_state(self._on_door_open, sensor, old="off", new="on", doorroom=room)
                    # Register door close handler
                    self.listen_state(self._on_door_close, sensor, old="on", new="off", doorroom=room)
                    self.log(f"Registered door handlers for {room}", level="INFO")
                except Exception as e:
                    self.log(f"Error setting up door handler for {room}: {e}", level="ERROR")
            
            # Register rooftop door handler if configured
            if self.rooftop_door_sensor:
                try:
                    current_state = self.get_state(self.rooftop_door_sensor)
                    if current_state == "on":
                        self.log(f"Rooftop door is open at startup: {self.rooftop_door_sensor}", level="INFO")
                    else:
                        self.log(f"Rooftop door sensor: {self.rooftop_door_sensor} is {current_state}", level="DEBUG")
                    
                    # Register handlers with explicit rooftop flag
                    self.listen_state(self._on_door_open, self.rooftop_door_sensor, 
                                     old="off", new="on", doorroom="rooftop", is_rooftop=True)
                    self.listen_state(self._on_door_close, self.rooftop_door_sensor, 
                                     old="on", new="off", doorroom="rooftop", is_rooftop=True)
                    self.log("Registered rooftop door handlers", level="INFO")
                    
                    # For ongoing checks
                    if current_state == "on":
                        self.log("Important: rooftop door is open at startup!", level="INFO")
                except Exception as e:
                    self.log(f"Error setting up rooftop door handler: {e}", level="ERROR")
                    self.rooftop_door_sensor = None  # Clear it if there was an error
            
            # Register zone.home handler to detect when people come home or leave
            try:
                self.listen_state(self._on_home_state_change, "zone.home")
                current_home_state = self.get_state("zone.home")
                self.log(f"Registered zone.home handler - current state: {current_home_state}", level="INFO")
            except Exception as e:
                self.log(f"Error setting up zone.home handler: {e}", level="ERROR")

            if self.apartment_entry_door_sensor:
                try:
                    self.listen_state(
                        self._on_apartment_door_open_edge,
                        self.apartment_entry_door_sensor,
                        old="off",
                        new="on",
                    )
                    self.log(
                        f"Apartment entry door latch on open edge: {self.apartment_entry_door_sensor}",
                        level="INFO",
                    )
                except Exception as e:
                    self.log(f"Error registering apartment entry door listener: {e}", level="ERROR")

            for lt in sorted(self._manual_bright_watch):
                try:
                    self.listen_state(
                        self._on_manual_bright_light_change,
                        lt,
                        old="off",
                        new="on",
                    )
                    self.log(f"Manual bright listener: {lt}", level="INFO")
                except Exception as e:
                    self.log(f"Error registering manual bright listener for {lt}: {e}", level="ERROR")

            if self._dishwasher_state_entity:
                try:
                    self.listen_state(self._on_dishwasher_state_change, self._dishwasher_state_entity)
                    self.log(
                        f"Dishwasher state listener: {self._dishwasher_state_entity} (island = group light.island_lights only)",
                        level="INFO",
                    )
                except Exception as e:
                    self.log(f"Dishwasher state listener failed: {e}", level="ERROR")
            
            # Validate and recover states after initialization
            self.run_in(self._validate_and_recover_states, self.initial_check_delay + 2)
            # Removed verbose initialization complete message for streamlined logging
            
        except Exception as e:
            self.log(f"CRITICAL ERROR: {e}", level="ERROR")
    


    def _on_raw_pir_on(self, entity, attribute, old, new, kwargs):
        """Fast path: evaluate before darkness_calculator room_state push."""
        try:
            room = kwargs.get("room", "unknown")
            self.log(f"Family room: PIR on in {room} - immediate evaluation", level="INFO")
            self._schedule_evaluation(immediate=True)
        except Exception as e:
            self.log(f"Error in PIR on handler: {e}", level="ERROR")

    def _on_raw_pir_off(self, entity, attribute, old, new, kwargs):
        """Handle PIR/presence sensor OFF - re-evaluate lights."""
        try:
            room = kwargs.get("room", "unknown")
            self.log(f"Family room: Presence lost in {room}", level="INFO")
            if not self._has_family_room_presence():
                self._clear_door_arrival_latch("all family PIR off")
                self._sleep_activated_during_presence = False
            # Kitchen off: evaluate immediately so island handoff from dishwasher_island_signal (dark solo
            # cleanup) is not delayed behind debounce while another app may have left only island_light_1 on.
            self._schedule_evaluation(immediate=(room == "kitchen"))
        except Exception as e:
            self.log(f"Error in PIR off handler: {e}", level="ERROR")

    def _is_perceived_dark(self):
        """Alias for committed dark - kept for callers; do not use ``pending_target`` here."""
        return self._is_confirmed_dark()
    
    def _is_sleep_mode_active(self):
        """Check if any sleep mode is active"""
        if not self.sleep_modes:
            return False
            
        try:
            for mode in self.sleep_modes:
                if self.get_state(mode) == "on":
                    self.log(f"Sleep mode active: {mode}", level="INFO")
                    return True
            return False
        except Exception as e:
            self.log(f"Error checking sleep modes: {e}", level="ERROR")
            return False
    
    def _is_everyone_home_sleeping(self):
        """Check if everyone who is home is in sleep mode and there's no family room motion"""
        try:
            # Use consolidated sleep mode status
            sleep_status = self._get_sleep_mode_status()
            
            # Check if there's any motion in main family zones (same set as lighting presence)
            family_room_motion = False
            for room, sensor in self._family_presence_sensors.items():
                try:
                    state = self.get_state(sensor)
                    if state is None:
                        self.log(f"Presence sensor {sensor} ({room}) is unavailable - treating as 'off'", level="WARNING")
                        continue  # Skip dead sensors
                    if state == "on":
                        family_room_motion = True
                        break
                except Exception as e:
                    self.log(f"Error checking family room motion in {room}: {e} - treating as 'off'", level="WARNING")
            
            # Everyone is sleeping AND no family room motion
            result = sleep_status['everyone_sleeping'] and not family_room_motion
            
            if result:
                self.log(f"Everyone home is sleeping ({', '.join(sleep_status['people_sleeping'])}) and no family room motion - all lights should be off", level="INFO")
            else:
                if self.log_level == "debug":
                    self.log(f"Not everyone sleeping: home={sleep_status['people_home']}, sleeping={sleep_status['people_sleeping']}, motion={family_room_motion}", level="DEBUG")
            
            return result
            
        except Exception as e:
            self.log(f"Error checking if everyone home is sleeping: {e} - defaulting to 'no one sleeping'", level="ERROR")
            return False  # Safe default
    
    def _is_bedroom_sleeper_active(self):
        """Return True if Mikkel sleep boolean is on (suppresses hallway lights)"""
        try:
            return self.get_state("input_boolean.mikkel_sleep_mode") == "on"
        except Exception as e:
            self.log(f"Error checking child sleep booleans: {e}", level="ERROR")
            return False

    def _get_sleep_entity_for_person(self, person):
        """Get sleep mode entity for a person"""
        if person == "person.kristine":
            return "input_boolean.kristine_sleep_mode"
        elif person == "person.mikkel":
            return "input_boolean.mikkel_sleep_mode"
        return None

    def _get_sleep_mode_status(self):
        """Get comprehensive sleep mode status - optimized with safe wrapper"""
        try:
            # Check if anyone is home - use safe wrapper to prevent blocking
            home_state = self._safe_get_state("zone.home", default="0", timeout_warning=False)
            if home_state is None or home_state == "0":
                return {
                    'anyone_sleeping': False,
                    'everyone_sleeping': False,
                    'sleep_mode_active': False,
                    'people_home': [],
                    'people_sleeping': []
                }
            
            # Get people who are home - use safe wrapper
            try:
                home_attributes = self.get_state("zone.home", attribute="persons")
            except Exception:
                home_attributes = None
            
            if not home_attributes:
                return {
                    'anyone_sleeping': False,
                    'everyone_sleeping': False,
                    'sleep_mode_active': False,
                    'people_home': [],
                    'people_sleeping': []
                }
            
            people_home = []
            people_sleeping = []
            sleep_mode_active = False
            
            for person in home_attributes:
                people_home.append(person)
                sleep_entity = self._get_sleep_entity_for_person(person)
                if sleep_entity:
                    # Use safe wrapper to prevent blocking on slow sleep mode checks
                    if self._safe_get_state(sleep_entity, default="off", timeout_warning=False) == "on":
                        people_sleeping.append(person)
                        sleep_mode_active = True
            
            everyone_sleeping = len(people_sleeping) == len(people_home) and len(people_home) > 0
            
            return {
                'anyone_sleeping': sleep_mode_active,
                'everyone_sleeping': everyone_sleeping,
                'sleep_mode_active': sleep_mode_active,
                'people_home': people_home,
                'people_sleeping': people_sleeping
            }
            
        except Exception as e:
            self.log(f"Error getting sleep mode status: {e} - defaulting to no sleep mode", level="ERROR")
            return {
                'anyone_sleeping': False,
                'everyone_sleeping': False,
                'sleep_mode_active': False,
                'people_home': [],
                'people_sleeping': []
            }

    def _is_dark_enough(self):
        """Committed dark only - same as ``sensor.darkness_*`` / ``(Dark)`` label (not ``pending_*``)."""
        return self._is_confirmed_dark()

    def _is_dark_for_auto_on(self):
        """Dark enough to turn on (pending dark or confirmed - from calculator)."""
        try:
            if self.room_state_text_entity:
                return room_state_darkness.evaluate_auto_on(
                    self,
                    self.room_state_text_entity,
                    default_dark=True,
                    darkness_sensor=self._darkness_confirmed_sensor,
                ).is_dark
        except Exception:
            pass
        return self._is_confirmed_dark()

    def _is_confirmed_dark(self):
        """
        True only after darkness_calculator has **committed** dark: ``sensor.darkness_*`` == ``dark``
        or room_state label contains ``(Dark)``. Unknown/unavailable -> assume dark (safe for lighting).
        """
        try:
            if self._darkness_confirmed_sensor:
                s = self._safe_get_state(self._darkness_confirmed_sensor, default=None, timeout_warning=False)
                if s == "dark":
                    return True
                if s == "bright":
                    return False
            if self.room_state_text_entity:
                st = self._safe_get_state(self.room_state_text_entity, default=None, timeout_warning=False)
                if isinstance(st, str) and st:
                    m = re.search(r"\((Dark|Bright)\)", st, re.I)
                    if m:
                        return m.group(1).lower() == "dark"
        except Exception:
            pass
        return True

    def _is_bright_for_auto_off(self):
        """Bright enough to turn off while occupied (pending_bright or confirmed)."""
        try:
            if self.room_state_text_entity:
                return not room_state_darkness.evaluate_auto_off(
                    self,
                    self.room_state_text_entity,
                    default_dark=True,
                    darkness_sensor=self._darkness_confirmed_sensor,
                ).is_dark
        except Exception:
            pass
        return self._is_confirmed_bright()

    def _is_confirmed_bright(self):
        """
        True only after darkness_calculator has **committed** bright.
        Same sources as ``_is_confirmed_dark`` - ``sensor.darkness_*`` first, then ``(Dark|Bright)`` label.
        """
        try:
            if self._darkness_confirmed_sensor:
                s = self._safe_get_state(self._darkness_confirmed_sensor, default=None, timeout_warning=False)
                if s == "bright":
                    return True
                if s == "dark":
                    return False
            if self.room_state_text_entity:
                st = self._safe_get_state(self.room_state_text_entity, default=None, timeout_warning=False)
                if isinstance(st, str) and st:
                    m = re.search(r"\((Dark|Bright)\)", st, re.I)
                    if m:
                        return m.group(1).lower() == "bright"
        except Exception:
            pass
        return False

    def _scan_raw_main_presence(self):
        """True if any PIR in ``_family_presence_sensors`` is ``on`` - same OR as the dashboard binaries."""
        for room, sensor in self._family_presence_sensors.items():
            st = self._safe_get_state(sensor, default="off", timeout_warning=False)
            if st == "on":
                return True, room
        return False, None

    def _is_dishwasher_unemptied(self):
        if not getattr(self, "_dishwasher_state_entity", None):
            return False
        return (
            self._safe_get_state(self._dishwasher_state_entity, default="", timeout_warning=False)
            == "Unemptied"
        )

    def _normal_island_group_entity(self):
        isl = self.light_map.get("island") or []
        return isl[0] if isl else None

    def _island_light_entities(self):
        """Always ``light.island_lights`` - one group for all island bulbs (no SG/bulb split)."""
        return list(self.light_map.get("island") or [])

    def _all_light_entities(self):
        """light_map['all'] with island as the single group entity."""
        return list(self.light_map.get("all") or [])

    def _island_entity_set(self):
        return set(self._island_light_entities())

    def _any_island_light_on(self):
        for eid in self._island_light_entities():
            if self._safe_get_state(eid, default="off", timeout_warning=False) == "on":
                return True
        return False

    def _skip_island_power_on_unemptied_bright(self):
        """Unemptied + bright: do not power island here; dishwasher_island_signal drives full group on kitchen PIR."""
        return self._is_dishwasher_unemptied() and not self._is_dark_enough()

    def _kitchen_pir_on(self):
        k = self.presence.get("kitchen") if isinstance(self.presence, dict) else None
        if not k:
            return False
        return self._safe_get_state(k, default="off", timeout_warning=False) == "on"

    def _turn_off_exempt_dishwasher_signal_lights(self):
        """Unemptied + kitchen PIR + bright: exempt ``light.island_lights`` (dishwasher drives full green). Dark signal uses only SG/bulb1 - not in this app's light_map, so no exempt needed."""
        if not self._is_dishwasher_unemptied() or not self._kitchen_pir_on():
            return set()
        if not self._is_dark_enough():
            nid = self._normal_island_group_entity()
            return {nid} if nid else set()
        return set()

    def _on_dishwasher_state_change(self, entity, attribute, old, new, kwargs):
        try:
            old_u = old == "Unemptied"
            new_u = new == "Unemptied"
            nid = self._normal_island_group_entity()
            if old_u and not new_u and nid:
                try:
                    if self._safe_get_state(nid, default="off", timeout_warning=False) == "on":
                        self._ad_turn_off(nid)
                except Exception as ie:
                    self.log(f"Dishwasher exit: off island group {nid}: {ie}", level="DEBUG")
            elif new_u and not old_u and nid:
                try:
                    if self._safe_get_state(nid, default="off", timeout_warning=False) == "on":
                        self._ad_turn_off(nid)
                except Exception as ie:
                    self.log(f"Dishwasher Unemptied: off full island {nid} for handoff: {ie}", level="DEBUG")
            self._schedule_evaluation()
        except Exception as e:
            self.log(f"_on_dishwasher_state_change: {e}", level="ERROR")

    def _activate_standby_mode(self):
        """Activate standby mode - island light only"""
        try:
            self.log("Activating standby mode - island light only", level="INFO")
            iset = self._island_entity_set()
            for light in self._all_light_entities():
                if light not in iset:
                    try:
                        self._ad_turn_off(light)
                    except Exception as e:
                        self.log(f"Error turning off {light}: {e}", level="ERROR")

            if not self.light_map.get("island"):
                return
            to_on = [] if self._skip_island_power_on_unemptied_bright() else self._island_light_entities()
            for island_light in to_on:
                try:
                    self._ad_turn_on(island_light)
                except Exception as e:
                    self.log(f"Error turning on island light {island_light}: {e}", level="ERROR")
        except Exception as e:
            self.log(f"Error activating standby mode: {e}", level="ERROR")
    
    def _handle_presence_lost(self):
        """Handle when family room presence is lost"""
        try:
            self._clear_door_arrival_latch("presence lost handler")
            # Presence session ended; allow sleep mode rules to take effect next time
            self._sleep_activated_during_presence = False
            # Always defer to the unified decision tree for consistent, race-free behavior
            self.log("Presence lost - using decision tree", level="INFO")
            self._schedule_evaluation()
        except Exception as e:
            self.log(f"Error handling presence lost: {e}", level="ERROR")
    
    def _check_adjacent_door_with_presence(self):
        """Check if any adjacent room has open door with presence"""
        try:
            if not self.doors or not self.adjacent_presence:
                return False
                
            for room in self.adjacent_rooms:
                if room in self.doors and room in self.adjacent_presence:
                    door_state = self.get_state(self.doors[room])
                    if room not in self.adjacent_presence:
                        continue
                    presence_state = self.get_state(self.adjacent_presence[room])

                    # Handle dead sensors
                    if door_state is None:
                        self.log(f"Door sensor for {room} is dead - treating as 'closed'", level="WARNING")
                        door_state = "off"
                    if presence_state is None:
                        self.log(f"Presence sensor for {room} is dead - treating as 'no presence'", level="WARNING")
                        presence_state = "off"
                    
                    # Debug logging for adjacent door presence check
                    if self.log_level == "debug":
                        self.log(f"Adjacent check {room}: door={door_state}, presence={presence_state}", level="DEBUG")
                    
                    if door_state == "on" and presence_state == "on":
                        self.log(f"Adjacent door with presence detected: {room} (door={door_state}, presence={presence_state})", level="INFO")
                        return True
            return False
        except Exception as e:
            self.log(f"Error checking adjacent door with presence: {e} - defaulting to 'no adjacent presence'", level="ERROR")
            return False  # Safe default
    
    def _is_rooftop_door_open(self):
        """Check if rooftop door is open"""
        try:
            if not self.rooftop_door_sensor:
                return False
            state = self.get_state(self.rooftop_door_sensor)
            if state is None:
                self.log("Rooftop door sensor is unavailable - treating as 'closed'", level="WARNING")
                return False
            return state == "on"
        except Exception as e:
            self.log(f"Error checking rooftop door: {e} - treating as 'closed'", level="ERROR")
            return False

    def _is_anyone_home(self):
        """Check if anyone is home with error handling"""
        try:
            home_state = self.get_state("zone.home")
            if home_state is None:
                self.log("zone.home is unavailable - assuming someone may be home", level="WARNING")
                return True  # Do not force lights off on ambiguous state
            return home_state != "0"
        except Exception as e:
            self.log(f"Error checking if anyone is home: {e} - defaulting to 'no one home'", level="ERROR")
            return False  # Safe default
    
    
    
    def _handle_door_open(self, doorroom, is_rooftop):
        """Handle door open events"""
        try:
            if lighting_actions.manual_override_active(self, getattr(self, "manual_override_entity", None)):
                return
            # Check for family room presence
            if self._has_family_room_presence():
                # Family room presence takes priority
                return
            
            # Handle rooftop door specially
            if is_rooftop:
                self.log("Rooftop door open - no action needed", level="INFO")
                return
            
            # Regular door with presence - activate standby mode
            if self._check_adjacent_door_with_presence():
                # If everyone is sleeping, turn off all lights instead of standby
                if self._is_everyone_home_sleeping():
                    self.log(f"Everyone sleeping with {doorroom} door presence - turning off all lights", level="INFO")
                    for light in self._all_light_entities():
                        self._ad_turn_off(light)
                # If it's confirmed bright, turn off all lights instead of standby
                elif self._is_confirmed_bright():
                    self.log(f"{doorroom} door with presence but confirmed bright - turning off all lights", level="INFO")
                    ex = self._turn_off_exempt_dishwasher_signal_lights()
                    for light in self._all_light_entities():
                        if light not in ex:
                            self._ad_turn_off(light)
                else:
                    self.log(f"{doorroom} door with presence - activating standby mode", level="INFO")
                    self._activate_standby_mode()
                return
            
            # No special conditions - let presence lost handler deal with it
            self._handle_presence_lost()
            
        except Exception as e:
            self.log(f"Error handling door open: {e}", level="ERROR")
    
    def _handle_door_close(self):
        """Handle door close events"""
        try:
            if lighting_actions.manual_override_active(self, getattr(self, "manual_override_entity", None)):
                return
            self.log("Door close handler: checking conditions", level="INFO")
            
            # Check for family room presence
            if self._has_family_room_presence():
                self.log("Door close handler: family room presence detected - no action needed", level="INFO")
                return
            
            # Check for other adjacent doors with presence
            adjacent_presence = self._check_adjacent_door_with_presence()
            self.log(f"Door close handler: adjacent door with presence = {adjacent_presence}", level="INFO")
            
            if adjacent_presence:
                # If everyone is sleeping, turn off all lights instead of standby
                if self._is_everyone_home_sleeping():
                    self.log("Everyone sleeping with adjacent door presence - turning off all lights", level="INFO")
                    for light in self._all_light_entities():
                        self._ad_turn_off(light)
                # If it's confirmed bright, turn off all lights instead of standby
                elif self._is_confirmed_bright():
                    self.log("Other adjacent door with presence but confirmed bright - turning off all lights", level="INFO")
                    ex = self._turn_off_exempt_dishwasher_signal_lights()
                    for light in self._all_light_entities():
                        if light not in ex:
                            self._ad_turn_off(light)
                else:
                    self.log("Other adjacent door with presence - activating standby mode", level="INFO")
                    self._activate_standby_mode()
                return
            
            # Check for rooftop door open
            rooftop_open = self._is_rooftop_door_open()
            self.log(f"Door close handler: rooftop door open = {rooftop_open}", level="INFO")
            
            if rooftop_open:
                # First check if anyone is home
                if not self._is_anyone_home():
                    self.log("No one is home and rooftop door is open - turning off all lights", level="INFO")
                    for light in self._all_light_entities():
                        self._ad_turn_off(light)
                    return
                
                # If everyone is sleeping, turn off all lights including living room
                if self._is_everyone_home_sleeping():
                    self.log("Everyone sleeping with rooftop door open - turning off all lights", level="INFO")
                    for light in self._all_light_entities():
                        self._ad_turn_off(light)
                else:
                    self.log("Rooftop door still open - preserving living room lights", level="INFO")
                    self._preserve_living_room_lights_only()
                return
            
            # No conditions met - turn off all lights
            self.log("No conditions met - turning off all lights", level="INFO")
            ex = self._turn_off_exempt_dishwasher_signal_lights()
            for light in self._all_light_entities():
                if light not in ex:
                    self._ad_turn_off(light)
                
        except Exception as e:
            self.log(f"Error handling door close: {e}", level="ERROR")
    
    def _handle_home_state_change(self):
        """Handle home state changes"""
        try:
            # Check for family room presence
            if self._has_family_room_presence():
                if self._is_dark_enough():
                    self.log("Family room presence and dark - turning on lights", level="INFO")
                    # The new decision tree handles this
                return
            
            # No family room presence - handle like presence lost
            self._handle_presence_lost()
            
        except Exception as e:
            self.log(f"Error handling home state change: {e}", level="ERROR")
    
    def _has_family_room_presence(self):
        """OR of configured family-zone PIRs - no AppDaemon delay; tune timing only in HA/sensors."""
        try:
            on, _ = self._scan_raw_main_presence()
            return bool(on)
        except Exception as e:
            self.log(f"Error checking family room presence: {e} - defaulting to 'no presence'", level="ERROR")
            return False
    
    def _schedule_evaluation(self, immediate=False):
        """
        Schedule a debounced evaluation. Supersedes any previous pending evaluation via _eval_token
        so we do not call cancel_timer() (avoids AppDaemon "Invalid callback handle" warnings).

        Args:
            immediate: If True, call _check_lights() directly (bypasses debounce).
                       Rate limiting inside _check_lights() still applies for safety.
                       Use for PIR triggers that need fast response.
        """
        self._eval_token += 1
        scheduled_t = self._eval_token

        if immediate:
            self._check_lights({"_scheduled_t": scheduled_t})
            return

        self._evaluation_timer = self.run_in(
            self._check_lights,
            self._evaluation_debounce_delay,
            _scheduled_t=scheduled_t,
        )

    def _check_lights(self, kwargs=None):
        """
        Main decision tree for family room lighting.
        Uses a clear hierarchical decision structure with a single exit point.
        """
        kwargs = kwargs or {}
        if kwargs.get("_scheduled_t") is not None and kwargs["_scheduled_t"] != self._eval_token:
            return

        # Clear the timer handle since we're now executing (debounce path)
        self._evaluation_timer = None

        # Rate limiting: prevent excessive evaluations (minimum interval between actual evaluations)
        now = time.time()
        if now - self._last_evaluation_time < self._min_evaluation_interval:
            # Too soon since last evaluation, reschedule for later
            st = self._eval_token
            self._evaluation_timer = self.run_in(
                self._check_lights,
                self._min_evaluation_interval - (now - self._last_evaluation_time),
                _scheduled_t=st,
            )
            return

        self._last_evaluation_time = now
        try:
            # Step 1: Gather all current states (no decisions yet)
            context = self._gather_lighting_context()
            if self._maybe_clear_door_arrival_latch():
                context["apartment_entry_active"] = self._apartment_entry_signal_active()

            # Step 2: Apply decision tree and determine required action
            action = self._determine_lighting_action(context)
            
            # Step 3: Execute the determined action
            self._execute_lighting_action(action, context)

            if self._diag_sensor:
                self._diag_rollover_if_needed()
                self._diag_eval_today += 1
                self._diag_eval_session += 1
                self._publish_diagnostics(context, action)
            
        except Exception as e:
            self.log(f"Error in light check: {e}", level="ERROR")

    def _safe_get_state(self, entity_id, default=None, timeout_warning=True, max_wait=2.0):
        """
        Safely get state with error handling and timeout protection.
        Returns default if call fails, times out, or takes too long.
        max_wait: Maximum seconds to wait (default 2.0s to prevent blocking)
        """
        import time as time_module
        start = time_module.time()
        try:
            state = self.get_state(entity_id)
            elapsed = time_module.time() - start
            # If call took too long, log warning but return the result
            if elapsed > max_wait and timeout_warning:
                self.log(f"Slow get_state for {entity_id}: took {elapsed:.2f}s", level="WARNING")
            return state
        except Exception as e:
            if timeout_warning:
                self.log(f"Error getting state for {entity_id}: {e}", level="WARNING")
            return default

    def _clear_door_arrival_latch(self, reason=""):
        if not self._door_arrival_latch:
            return
        self._door_arrival_latch = False
        self._latch_zone_persons_snapshot = None
        if reason:
            self.log(f"Door arrival latch cleared: {reason}", level="INFO")

    def _zone_arrival_resolves_latch(self):
        if not self._door_arrival_latch or self._latch_zone_persons_snapshot is None:
            return False
        try:
            persons = self.get_state("zone.home", attribute="persons") or []
        except Exception:
            persons = []
        current = frozenset(persons)
        snap = self._latch_zone_persons_snapshot
        if len(current) > len(snap):
            return True
        if snap != current and snap.issubset(current):
            return True
        return False

    def _guest_exit_door_rooms(self):
        if self.guest_exit_use_adjacent_doors_only:
            return [r for r in self.adjacent_rooms if r in (self.doors or {})]
        return list((self.doors or {}).keys())

    def _guest_exit_satisfied(self):
        if self._has_family_room_presence():
            return False
        for room in self._guest_exit_door_rooms():
            door = self.doors.get(room)
            if not door:
                continue
            st = self._safe_get_state(door, default="off", timeout_warning=False)
            if st is None:
                st = "off"
            if st == "on":
                return False
        if self.guest_exit_require_entry_door_closed and self.apartment_entry_door_sensor:
            ent = self._safe_get_state(self.apartment_entry_door_sensor, default="off", timeout_warning=False)
            if ent == "on":
                return False
        return True

    def _maybe_clear_door_arrival_latch(self):
        if not self._door_arrival_latch:
            return False
        if self._zone_arrival_resolves_latch():
            self._clear_door_arrival_latch("zone.home persons expanded")
            return True
        if self._guest_exit_satisfied():
            self._clear_door_arrival_latch("guest exit (adjacent doors closed, no family PIR)")
            return True
        return False

    def _apartment_entry_signal_active(self):
        if not self.apartment_entry_door_sensor:
            return False
        door_on = self._safe_get_state(self.apartment_entry_door_sensor, default="off", timeout_warning=False) == "on"
        return bool(self._door_arrival_latch or door_on)

    def _on_apartment_door_open_edge(self, entity, attribute, old, new, kwargs):
        try:
            self._door_arrival_latch = True
            try:
                persons = self.get_state("zone.home", attribute="persons") or []
            except Exception:
                persons = []
            self._latch_zone_persons_snapshot = frozenset(persons)
            self.log("Apartment door opened - arrival latch set", level="INFO")
            self._schedule_evaluation(immediate=True)
        except Exception as e:
            self.log(f"Error in apartment door open handler: {e}", level="ERROR")

    def _get_light_context_user_id(self, entity_id):
        try:
            full = self.get_state(entity_id, attribute="all")
            if not full or not isinstance(full, dict):
                return None
            ctx = full.get("context")
            if isinstance(ctx, dict):
                return ctx.get("user_id")
        except Exception:
            pass
        return None

    def _manual_bright_echo_active(self, entity_id):
        until = self._manual_bright_echo_until.get(entity_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._manual_bright_echo_until[entity_id]
            return False
        return True

    def _note_app_light_command(self, entity_id):
        if entity_id in self._manual_bright_watch:
            self._manual_bright_echo_until[entity_id] = (
                time.monotonic() + self._manual_bright_echo_seconds
            )

    def _zone_manual_active(self, light):
        """GLOBAL mechanism, per zone: True when this light belongs to a light_map group
        whose manual-override boolean is ON - the app must not touch that light. All
        actuation funnels through _ad_turn_on/_ad_turn_off, so every code path
        (decisions, standby, door handlers, enforcement) respects it."""
        try:
            overrides = getattr(self, "manual_override_booleans", None) or {}
            if not overrides:
                return False
            for grp, ent in overrides.items():
                if ent and light in self.light_map.get(grp, []):
                    if self.get_state(ent) == "on":
                        return True
            return False
        except Exception:
            return False

    def _ad_turn_on(self, light):
        if self._zone_manual_active(light):
            self.log(f"Skip auto-ON {light}: zone manual override", level="DEBUG")
            return
        self._diag_track_physical("on")
        self.turn_on(light)
        self._note_app_light_command(light)

    def _ad_turn_off(self, light):
        if self._zone_manual_active(light):
            self.log(f"Skip auto-OFF {light}: zone manual override", level="DEBUG")
            return
        self._diag_track_physical("off")
        self.turn_off(light)
        self._note_app_light_command(light)

    def _diag_rollover_if_needed(self):
        d = datetime.date.today()
        if self._diag_day != d:
            self._diag_day = d
            self._diag_phys_on_today = 0
            self._diag_phys_off_today = 0
            self._diag_eval_today = 0

    def _diag_track_physical(self, direction):
        if not self._diag_sensor:
            return
        self._diag_rollover_if_needed()
        if direction == "on":
            self._diag_phys_on_today += 1
            self._diag_phys_on_session += 1
        else:
            self._diag_phys_off_today += 1
            self._diag_phys_off_session += 1

    def _get_room_state_snapshot(self):
        """Copy dark/bright thresholds and lux from darkness_calculator published room state."""
        ent = self.room_state_text_entity
        out = {"room_state_label": None}
        if not ent:
            return out
        try:
            out["room_state_label"] = self.get_state(ent)
        except Exception:
            pass
        for key in (
            "dark_threshold",
            "bright_threshold",
            "indoor_lux",
            "outdoor_lux",
            "band_zone",
            "pending_target",
            "pending_remaining_seconds",
            "pending_seconds_required",
            "reason",
            "day_type",
            "source_zone",
        ):
            try:
                v = self.get_state(ent, attribute=key)
                if v is not None:
                    out[key] = v
            except Exception:
                pass
        return out

    def _publish_diagnostics(self, context, action):
        if not self._diag_sensor:
            return
        self._diag_rollover_if_needed()
        snap = self._get_room_state_snapshot()
        label = snap.get("room_state_label") or "unknown"
        if isinstance(label, str) and len(label) > 250:
            label = label[:247] + "..."

        if action:
            self._diag_last_action = action.get("action")
            self._diag_last_reason = action.get("reason")
            self._diag_last_details = (action.get("details") or "")[:500]

        attrs = {
            "dark_threshold_lx": snap.get("dark_threshold"),
            "bright_threshold_lx": snap.get("bright_threshold"),
            "indoor_lux": snap.get("indoor_lux"),
            "outdoor_lux": snap.get("outdoor_lux"),
            "band_zone": snap.get("band_zone"),
            "pending_target": snap.get("pending_target"),
            "pending_remaining_seconds": snap.get("pending_remaining_seconds"),
            "darkness_reason": snap.get("reason"),
            "day_type": snap.get("day_type"),
            "lighting_action": self._diag_last_action,
            "lighting_reason": self._diag_last_reason,
            "lighting_details": self._diag_last_details[:200] if self._diag_last_details else None,
            "physical_toggles_on_today": self._diag_phys_on_today,
            "physical_toggles_off_today": self._diag_phys_off_today,
            "physical_toggles_on_session": self._diag_phys_on_session,
            "physical_toggles_off_session": self._diag_phys_off_session,
            "evaluations_today": self._diag_eval_today,
            "evaluations_session": self._diag_eval_session,
            # Keep both committed and auto-on darkness visible for hybrid debugging.
            "is_dark_committed": context.get("is_dark") if context else None,
            "is_dark_for_auto_on": context.get("is_dark_for_auto_on") if context else None,
            "is_confirmed_bright": context.get("is_confirmed_bright") if context else None,
            "family_presence": context.get("family_presence") if context else None,
            "updated": datetime.datetime.now().isoformat(),
            "friendly_name": "Family room lights diagnostics",
            "icon": "mdi:chart-box-outline",
        }
        try:
            # physical_toggles_on/off_today/session (0 until the app's first toggle today/this
            # session) and is_dark_committed/is_dark_for_auto_on/is_confirmed_bright/
            # family_presence (False whenever bright/empty - the routine case) silently drop
            # from published attributes whenever they're False/0 -- AppDaemon 4.5.13 set_state
            # bug, not ours; see smart_cooling.py's _publish() for details.
            self.set_state(self._diag_sensor, state=label, attributes=attrs, replace=True)
        except Exception as e:
            self.log(f"diagnostics set_state failed: {e}", level="DEBUG")

    def _on_manual_bright_light_change(self, entity, attribute, old, new, kwargs):
        try:
            if entity not in self._manual_bright_watch:
                return
            if self._manual_bright_echo_active(entity):
                return
            uid = self._get_light_context_user_id(entity)
            if not uid:
                return
            if not self._has_family_room_presence():
                return
            self.log(
                f"Manual bright (user context) on {entity} - scheduling evaluation",
                level="INFO",
            )
            if not self._sleep_activated_during_presence:
                self._sleep_activated_during_presence = True
                self.log(
                    "User brightened sit-in lights - preserving normal lighting for this presence session",
                    level="INFO",
                )
            self._schedule_evaluation()
        except Exception as e:
            self.log(f"Error in manual bright handler: {e}", level="ERROR")

    def _gather_lighting_context(self):
        """Gather all current states and conditions - no decisions, just facts"""
        context = {
            'family_presence': False,
            'presence_room': None,
            'adjacent_door_with_presence': False,
            'adjacent_room_with_presence': None,
            'adjacent_debug_info': {},
            'rooftop_door_open': False,
            'anyone_home': False,
            'sleep_status': None,
            'is_dark': False,
            'is_dark_for_auto_on': False,
            'is_confirmed_bright': False,
            'current_lights_on': False,
            'island_light_on': False,
            'other_lights_on': False,
            'offline_sensors': [],
            'apartment_entry_active': False,
        }
        
        raw_on, pres_room = self._scan_raw_main_presence()
        context['family_presence'] = raw_on
        if pres_room:
            context['presence_room'] = pres_room

        # Check adjacent doors with presence - optimized with safe wrapper
        if self.doors and self.adjacent_presence:
            for room in self.adjacent_rooms:
                if room in self.doors and room in self.adjacent_presence:
                    door_state = self._safe_get_state(self.doors[room], default=None, timeout_warning=False)
                    if door_state is None:
                        context['offline_sensors'].append(f"door_{room}")
                        context['adjacent_debug_info'][room] = {'door': None, 'presence': None}
                        continue

                    presence_state = self._safe_get_state(self.adjacent_presence[room], default=None, timeout_warning=False)
                    if presence_state is None:
                        context['offline_sensors'].append(f"presence_{room}")
                        context['adjacent_debug_info'][room] = {'door': door_state, 'presence': None}
                        continue
                    
                    # Capture debug info for visibility regardless of match
                    context['adjacent_debug_info'][room] = {'door': door_state, 'presence': presence_state}

                    if door_state == "on" and presence_state == "on":
                        context['adjacent_door_with_presence'] = True
                        context['adjacent_room_with_presence'] = room
                        break  # Early exit - found what we need
        
        # Check rooftop door - use safe wrapper
        if self.rooftop_door_sensor:
            rooftop_state = self._safe_get_state(self.rooftop_door_sensor, default=None, timeout_warning=False)
            if rooftop_state is None:
                context['offline_sensors'].append("rooftop_door")
            elif rooftop_state == "on":
                context['rooftop_door_open'] = True
        
        # Check if anyone is home - use safe wrapper
        home_state = self._safe_get_state("zone.home", default="0", timeout_warning=False)
        context['anyone_home'] = (home_state != "0")
        
        # Get sleep status
        context['sleep_status'] = self._get_sleep_mode_status()
        
        # Committed dark/bright from darkness_calculator for auto-on and auto-off.
        context['is_dark'] = self._is_dark_enough()
        context['is_dark_for_auto_on'] = self._is_dark_for_auto_on()
        context['is_confirmed_bright'] = self._is_confirmed_bright()
        context['is_bright_for_auto_off'] = self._is_bright_for_auto_off()
        
        # Check current light states - optimized with early exits
        context['current_lights_on'] = False
        context['island_light_on'] = False
        context['other_lights_on'] = False
        
        # Island = current mode's island entity set (full group or SG + bulb 1 when Unemptied)
        if self._any_island_light_on():
            context['island_light_on'] = True
            context['current_lights_on'] = True

        if not context['island_light_on']:
            iset = self._island_entity_set()
            for light in self._all_light_entities():
                if light in iset:
                    continue
                if self._safe_get_state(light, default="off", timeout_warning=False) == "on":
                    context['current_lights_on'] = True
                    context['other_lights_on'] = True
                    break

        context["apartment_entry_active"] = self._apartment_entry_signal_active()

        return context

    def _note_darkness_committed_changed(self):
        """Re-evaluate when committed dark/bright changes (not ``pending_target``)."""
        new_dark = self._is_confirmed_dark()
        if not new_dark:
            self._sleep_activated_during_presence = False
        if new_dark != self._dark_flag:
            self._dark_flag = new_dark
            self._last_darkness_state = new_dark
            self._schedule_evaluation()

    def _on_room_state_push(self, entity, attribute, old, new, kwargs):
        """React when darkness_calculator pushes (lux, presence, pending, confirmed)."""
        try:
            if attribute == "pending_target":
                self._schedule_evaluation()
                return
            # Enter/leave must re-run the tree (not only when dark↔bright flips).
            if attribute in (None, "occupied"):
                immediate = False
                if attribute == "occupied":
                    immediate = str(new).lower() in ("true", "on", "1", "yes")
                elif isinstance(new, str) and new:
                    immediate = "occupied" in new.lower()
                self._schedule_evaluation(immediate=immediate)
            self._note_darkness_committed_changed()
        except Exception:
            pass

    def _on_darkness_committed_change(self, entity, attribute, old, new, kwargs):
        """``sensor.darkness_*`` state flipped dark ↔ bright."""
        try:
            if old == new:
                return
            self._note_darkness_committed_changed()
        except Exception:
            pass

    def _parse_dark_from_room_state(self, state_text: str) -> bool:
        """Parse ``(Dark|Bright)`` from label text."""
        try:
            m = re.search(r"\((dark|bright)\)", str(state_text), re.I)
            if m:
                return m.group(1).lower() == "dark"
        except Exception:
            pass
        return self._dark_flag if self._dark_flag is not None else True

    def _determine_lighting_action(self, context):
        """
        Decision tree to determine what lighting action to take.
        Returns a dict with action type and details.
        All conditions are mutually exclusive - exactly one will match.
        """
        # Log offline sensors if any
        if context['offline_sensors']:
            self.log(f"Offline sensors detected: {', '.join(context['offline_sensors'])}", level="WARNING")
        
        # -1. GLOBAL mechanism: manual override - human owns the lights, automation fully paused
        if lighting_actions.manual_override_active(self, getattr(self, "manual_override_entity", None)):
            return {
                'action': 'preserve_current_state',
                'reason': 'manual_override',
                'details': 'Manual override toggle is on - automatic lighting paused'
            }

        # DECISION TREE - Mutually exclusive conditions:

        # 0. Adjacent room door+PIR while everyone sleeping, no family PIR, dark (would match branch 1 before branch 6)
        if (
            context["sleep_status"]["everyone_sleeping"]
            and not context["family_presence"]
            and context["adjacent_door_with_presence"]
            and context["is_dark_for_auto_on"]
        ):
            return {
                "action": "turn_on_island_only",
                "reason": "adjacent_activity_everyone_sleeping_dark",
                "details": (
                    f"Adjacent room activity ({context['adjacent_room_with_presence']}) while everyone sleeping - island standby"
                ),
            }

        # 1. Everyone sleeping and no family room motion
        if context['sleep_status']['everyone_sleeping'] and not context['family_presence']:
            return {
                'action': 'turn_off_all',
                'reason': 'everyone_sleeping_no_motion',
                'details': f"Everyone sleeping ({', '.join(context['sleep_status']['people_sleeping'])}) and no family room motion"
            }
        
        # 2. No one home (rooftop door no longer special - user 2026-07-06)
        if not context['anyone_home'] and not context['family_presence']:
            return {
                'action': 'turn_off_all',
                'reason': 'no_one_home',
                'details': 'No one is home'
            }
        
        # 3. Family presence (regardless of sleep status)
        if context['family_presence']:
            # Bright first: only after **confirmed** bright (matches calculator timers; avoids pending/lux flap).
            if context['is_bright_for_auto_off']:
                self._sleep_activated_during_presence = False
                return {
                    'action': 'turn_off_all',
                    'reason': 'family_presence_bright',
                    'details': 'Family presence but bright - no need for lights',
                }
            # If sleep mode was activated during this continuous presence session,
            # preserve the current lighting until presence is lost (dark only; bright handled above).
            if self._sleep_activated_during_presence:
                return {
                    'action': 'preserve_current_state',
                    'reason': 'sleep_activated_during_presence',
                    'details': 'Sleep mode activated during ongoing presence - preserving current lighting until presence is lost'
                }
            if context['is_dark_for_auto_on']:
                # Dark with family presence - check sleep mode
                if context['sleep_status']['everyone_sleeping']:
                    if context.get("apartment_entry_active"):
                        if context["other_lights_on"]:
                            return {
                                "action": "preserve_current_state",
                                "reason": "apartment_entry_sleep_manual_or_lit",
                                "details": (
                                    "Apartment entry signal with family PIR while everyone sleeping - preserving brighter/manual state"
                                ),
                            }
                        return {
                            "action": "turn_on_island_only",
                            "reason": "apartment_entry_everyone_sleeping_dark",
                            "details": (
                                "Door open or arrival latch + family PIR while everyone sleeping - island welcome"
                            ),
                        }
                    # Everyone is sleeping but there's still family presence
                    # This means sleep mode was activated while presence was continuous
                    # We should preserve the current lighting state until presence is lost
                    return {
                        'action': 'preserve_current_state',
                        'reason': 'family_presence_dark_everyone_sleeping_waiting',
                        'details': f"Family presence with everyone sleeping ({', '.join(context['sleep_status']['people_sleeping'])}) - preserving current state until presence lost"
                    }
                else:
                    # Dark with family presence, not everyone sleeping
                    if context['sleep_status']['anyone_sleeping']:
                        # If non-island lights are detected on, user has active control/normal lighting.
                        # Disable sleep mode restrictions for this session.
                        if context['other_lights_on']:
                            # Latch the session flag so we don't force island-only if the user toggles lights
                            if not self._sleep_activated_during_presence:
                                self._sleep_activated_during_presence = True
                                self.log("User restored normal lighting - disabling sleep mode restrictions for this session", level="INFO")
                                
                            return {
                                'action': 'preserve_current_state',
                                'reason': 'family_presence_dark_user_override',
                                'details': f"Family presence, dark, some sleeping ({', '.join(context['sleep_status']['people_sleeping'])}) - user has active lights, preserving state"
                            }

                        return {
                            'action': 'turn_on_island_only',
                            'reason': 'family_presence_dark_some_sleeping',
                            'details': f"Family presence, dark, some sleeping ({', '.join(context['sleep_status']['people_sleeping'])}) - sleep mode reduces lighting to island only"
                        }
                    else:
                        return {
                            'action': 'turn_on_all',
                            'reason': 'family_presence_dark_no_sleeping',
                            'details': 'Family presence, dark, no one sleeping'
                        }
            # Presence still true but not confirmed bright and responsive says "not dark" (e.g. pending bright
            # / lux band) - must never fall through to branch 7 default turn_off_all (misleading no_conditions_met).
            return {
                'action': 'preserve_current_state',
                'reason': 'family_presence_daylight_pending',
                'details': (
                    'Family presence: not dark enough for turn-on rules; daylight not yet confirmed bright - '
                    'preserving lights (avoids default turn_off)'
                ),
            }
        
        # 4. Rooftop door open (no family presence, someone home)
        if False and context['rooftop_door_open'] and context['anyone_home']:  # DISABLED: rooftop door no longer drives lights (user 2026-07-06)
            # If it's confirmed bright or everyone is sleeping, turn off all lights
            if context['sleep_status']['everyone_sleeping'] or context['is_confirmed_bright']:
                return {
                    'action': 'turn_off_all',
                    'reason': 'rooftop_open_bright_or_everyone_sleeping',
                    'details': 'Rooftop door open but confirmed bright or everyone sleeping - turning off all lights'
                }
            # Otherwise, at night with some people awake, preserve living room lights
            return {
                'action': 'preserve_living_room_only',
                'reason': 'rooftop_open_some_awake_dark',
                'details': 'Rooftop door open, dark, some people awake - preserve living room lights'
            }
        
        # 5. Rooftop door open, no one home
        if False and context['rooftop_door_open'] and not context['anyone_home']:  # DISABLED: rooftop door no longer drives lights
            return {
                'action': 'turn_off_all',
                'reason': 'rooftop_open_no_one_home',
                'details': 'Rooftop door open but no one home'
            }
        
        # 6. Adjacent door with presence (no family presence, no rooftop door)
        if context['adjacent_door_with_presence']:
            if context['sleep_status']['everyone_sleeping']:
                return {
                    'action': 'turn_off_all',
                    'reason': 'adjacent_door_everyone_sleeping',
                    'details': f"Adjacent door with presence but everyone sleeping ({', '.join(context['sleep_status']['people_sleeping'])})"
                }
            elif context['is_dark_for_auto_on']:
                return {
                    'action': 'turn_on_island_only',
                    'reason': 'adjacent_door_dark',
                    'details': f"Adjacent door with presence in {context['adjacent_room_with_presence']}, dark - standby mode | states: {context['adjacent_debug_info']}"
                }
            elif context['is_bright_for_auto_off']:
                return {
                    'action': 'turn_off_all',
                    'reason': 'adjacent_door_bright',
                    'details': f"Adjacent door with presence in {context['adjacent_room_with_presence']} but bright | states: {context['adjacent_debug_info']}"
                }
            else:
                return {
                    'action': 'preserve_current_state',
                    'reason': 'adjacent_door_daylight_pending',
                    'details': f"Adjacent door with presence - daylight not yet confirmed bright; avoiding off flap | states: {context['adjacent_debug_info']}"
                }
        
        # 7. DEFAULT: No special conditions - turn off all lights
        # This covers: someone home, no family presence, no rooftop door, no adjacent doors
        return {
            'action': 'turn_off_all',
            'reason': 'no_conditions_met',
            'details': 'Someone home but no presence, no open doors, no special conditions'
        }

    def _execute_lighting_action(self, action, context):
        """Execute the determined lighting action"""
        action_type = action['action']
        reason = action['reason']
        details = action['details']
        
        # Smart logging - only log if this is a new action or significant change
        should_log = self._should_log_action(action, context)
        
        if should_log:
            human_action = action_type.replace('_', ' ')
            self.log(f"Lighting decision: {human_action} - {details}", level="INFO")
        
        # Execute the action
        try:
            if action_type == 'turn_off_all':
                self._turn_off_all_lights()
            elif action_type == 'turn_on_all':
                self._turn_on_all_lights()
            elif action_type == 'turn_on_island_only':
                self._turn_on_island_only()
            elif action_type == 'preserve_living_room_only':
                self._preserve_living_room_only()
            elif action_type == 'preserve_current_state':
                # Do nothing - preserve current lighting state
                pass
            else:
                self.log(f"Unknown action type: {action_type}", level="ERROR")
        except Exception as e:
            self.log(f"Error executing action {action_type}: {e}", level="ERROR")

    def _should_log_action(self, action, context):
        """Determine if we should log this action (smart logging)"""
        # Always log if lights state will change
        current_lights_on = context['current_lights_on']
        
        if action['action'] == 'turn_off_all' and current_lights_on:
            return True
        elif action['action'] == 'turn_on_all' and not current_lights_on:
            return True
        elif action['action'] == 'turn_on_island_only':
            # Always log standby decisions so we can see the trigger evidence
            return True
        elif action['action'] == 'preserve_living_room_only' and context['other_lights_on']:
            return True
        elif action['action'] == 'preserve_current_state':
            # Log preserve_current_state actions less frequently to avoid spam
            self._action_log_count += 1
            if self._action_log_count >= 60:  # Log every ~30 minutes
                self._action_log_count = 0
                return True
            return False
        
        # Log periodic updates for ongoing conditions
        self._action_log_count += 1
        if self._action_log_count >= 30:  # Log every ~15 minutes
            self._action_log_count = 0
            return True
        
        return False

    def _turn_off_all_lights(self):
        """Turn off all family room lights (idempotent - only sends command if light is actually on)"""
        exempt = self._turn_off_exempt_dishwasher_signal_lights()
        for light in self._all_light_entities():
            if light in exempt:
                continue
            try:
                current_state = self._safe_get_state(light, default="off", timeout_warning=False)
                if current_state == "on":
                    self._ad_turn_off(light)
            except Exception as e:
                self.log(f"Error turning off {light}: {e}", level="ERROR")

    def _turn_on_all_lights(self):
        """Turn on all family room lights (idempotent - only sends command if light is actually off)"""
        hallway_lights = set(self.light_map.get("hallway", []))
        bedroom_sleep_active = self._is_bedroom_sleeper_active()
        for light in self._all_light_entities():
            try:
                # Suppress hallway lights while Mikkel is sleeping (light visible through door)
                if bedroom_sleep_active and light in hallway_lights:
                    current_state = self._safe_get_state(light, default="off", timeout_warning=False)
                    if current_state == "on":
                        self._ad_turn_off(light)
                    continue
                # Use safe wrapper and only turn on if actually off (idempotent)
                current_state = self._safe_get_state(light, default="off", timeout_warning=False)
                if current_state == "off":
                    self._ad_turn_on(light)
            except Exception as e:
                self.log(f"Error turning on {light}: {e}", level="ERROR")

    def _turn_on_island_only(self):
        """Turn on island light only (standby mode)"""
        if "island" not in self.light_map or not self.light_map["island"]:
            self.log("ERROR: Island light not configured in light_map!", level="ERROR")
            return

        iset = self._island_entity_set()
        exempt = self._turn_off_exempt_dishwasher_signal_lights()

        for light in self._all_light_entities():
            if light in iset:
                continue
            if light in exempt:
                continue
            try:
                current_state = self._safe_get_state(light, default="off", timeout_warning=False)
                if current_state == "on":
                    self._ad_turn_off(light)
                    self.log(f"Turned off {light} for standby mode", level="DEBUG")
            except Exception as e:
                self.log(f"Error turning off {light}: {e}", level="ERROR")

        to_on = [] if self._skip_island_power_on_unemptied_bright() else self._island_light_entities()
        for island_light in to_on:
            try:
                current_state = self._safe_get_state(island_light, default="off", timeout_warning=False)
                if current_state != "on":
                    self._ad_turn_on(island_light)
                    self.log(f"Turned on island light ({island_light}) for standby mode", level="INFO")
                else:
                    self.log(f"Island light ({island_light}) already on", level="DEBUG")
            except Exception as e:
                self.log(f"Error turning on island light {island_light}: {e}", level="ERROR")

    def _preserve_living_room_only(self):
        """Turn off all lights except living room lights"""
        living = set(self.light_map.get("living", []))
        for grp in ("dining", "kitchen", "hallway"):
            for light in self.light_map.get(grp, []):
                if light not in living:
                    try:
                        self._ad_turn_off(light)
                    except Exception as e:
                        self.log(f"Error turning off {light}: {e}", level="ERROR")
        for light in self._island_light_entities():
            if light not in living:
                try:
                    self._ad_turn_off(light)
                except Exception as e:
                    self.log(f"Error turning off {light}: {e}", level="ERROR")
    
    def _on_sleep_mode_change(self, entity, attribute, old, new, kwargs):
        """Handle sleep mode changes"""
        try:
            self.log(f"Sleep mode changed: {old} -> {new}", level="INFO")
            
            # If entering sleep mode, check for continuous presence
            if new == "on" and old == "off":
                # Check if there's continuous presence in the family room
                family_presence = self._has_family_room_presence()
                
                # If Mikkel sleep is activated, immediately turn off hallway lights
                if entity == "input_boolean.mikkel_sleep_mode":
                    for light in self.light_map.get("hallway", []):
                        try:
                            if self.get_state(light) == "on":
                                self._ad_turn_off(light)
                        except Exception as e:
                            self.log(f"Error turning off hallway light {light} on child sleep activation: {e}", level="ERROR")

                if family_presence:
                    # Mark that sleep mode was activated during the current presence session
                    self._sleep_activated_during_presence = True
                    self.log("Sleep mode activated but continuous presence detected - waiting for presence to be lost before applying sleep mode behavior", level="INFO")
                    
                    # IMPORTANT: Ensure we don't inadvertently turn off lights
                    # By returning without calling _check_lights(), we preserve current state.
                    # The state is now latched until presence is lost.
                    return
                else:
                    self.log("Sleep mode activated with no family room presence - applying sleep mode behavior immediately", level="INFO")
                    # No presence, so we can apply sleep mode behavior immediately
                    self._schedule_evaluation()
                    return
            
            # If exiting sleep mode, check for family presence to determine if lights should be on
            if new == "off" and old == "on":
                # Clear any pending preservation flag since sleep mode is off
                self._sleep_activated_during_presence = False
                # Do not trigger any lighting changes on sleep mode deactivation per requirement
                self.log("Sleep mode deactivated - no lighting changes", level="INFO")
                return
        except Exception as e:
            self.log(f"Error handling sleep mode change: {e}", level="ERROR")
    
    def _check_presence_with_fallback(self):
        """Check presence with dead sensor detection"""
        active_sensors = []
        dead_sensors = []
        all_sensors_off = True
        
        for room, sensor in self.presence.items():
            try:
                state = self.get_state(sensor)
                if state is None:
                    dead_sensors.append(room)
                    # Default to "off" for dead sensors
                    continue
                elif state == "on":
                    all_sensors_off = False
                    active_sensors.append(room)
            except Exception as e:
                dead_sensors.append(room)
                self.log(f"Sensor {sensor} ({room}) is dead: {e}", level="WARNING")
        
        # Log dead sensors
        if dead_sensors:
            self.log(f"Dead sensors detected: {', '.join(dead_sensors)} - treating as 'off'", level="WARNING")
        
        return {
            'all_sensors_off': all_sensors_off,
            'active_sensors': active_sensors,
            'dead_sensors': dead_sensors
        }

    def _on_adjacent_presence_change(self, entity, attribute, old, new, kwargs):
        """Handle adjacent room presence changes by re-evaluating lighting."""
        try:
            self.log(f"Adjacent presence changed: {entity} {old} -> {new}", level="INFO")
            self._schedule_evaluation()
        except Exception as e:
            self.log(f"Error in adjacent presence handler: {e}", level="ERROR")
    
    def _on_door_open(self, entity, attribute, old, new, kwargs):
        """Handle door opening event"""
        try:
            doorroom = kwargs.get("doorroom", "unknown")
            is_rooftop = kwargs.get("is_rooftop", False)
            
            if is_rooftop:
                self.log("Rooftop door opened", level="INFO")
            else:
                self.log(f"Door opened: {doorroom} door changed from {old} to {new}", level="INFO")
            
            # Use unified decision tree for consistent behavior
            self.log(f"Door open trigger: using unified decision tree", level="INFO")
            self._schedule_evaluation()
        except Exception as e:
            self.log(f"Error in door open handler: {e}", level="ERROR")
    
    def _on_door_close(self, entity, attribute, old, new, kwargs):
        """Handle door closing event"""
        try:
            doorroom = kwargs.get("doorroom", "unknown")
            is_rooftop = kwargs.get("is_rooftop", False)
            
            if is_rooftop:
                self.log("Rooftop door closed", level="INFO")
            else:
                self.log(f"Door closed: {doorroom} door changed from {old} to {new}", level="INFO")
            
            # Use unified decision tree for consistent behavior
            self.log(f"Door close trigger: using unified decision tree", level="INFO")
            self._schedule_evaluation()
        except Exception as e:
            self.log(f"Error in door close handler: {e}", level="ERROR")
    
    def _on_illuminance_change(self, entity, attribute, old, new, kwargs):
        """Handle illuminance changes (only for minor hysteresis; darkness from helper)."""
        try:
            # Skip if new value isn't valid for calculation
            try:
                if new is None:
                    self.log(f"Illuminance sensor {entity} returned None - skipping update", level="DEBUG")
                    return
                    
                new_val = float(new)
                if old is None:
                    self.log(f"Illuminance sensor {entity} old value is None - skipping comparison", level="DEBUG")
                    return
                    
                old_val = float(old)
                # Skip processing if the change is too small (add hysteresis)
                if abs(new_val - old_val) < self.illuminance_hysteresis:
                    return
            except (ValueError, TypeError) as e:
                self.log(f"Invalid illuminance value from {entity}: {e}", level="DEBUG")
                return
            
            # Do not recompute darkness here; helper drives darkness
            # If presence exists, small lux swings can still trigger a re-evaluation
            if self._has_family_room_presence():
                self._schedule_evaluation()
            
        except Exception as e:
            self.log(f"Error in illuminance handler: {e}", level="ERROR")
    
    def _on_hallway_light_state_change(self, entity, attribute, old, new, kwargs):
        """Enforce hallway stays off while child sleep mode is active"""
        try:
            if self._zone_manual_active(entity):
                return
            if self._is_bedroom_sleeper_active() and new == "on":
                self.log(f"Child sleep active - forcing hallway off: {entity}", level="INFO")
                self.turn_off(entity)
        except Exception as e:
            self.log(f"Error enforcing hallway off for {entity}: {e}", level="ERROR")

    def _on_home_state_change(self, entity, attribute, old, new, kwargs):
        """Handle zone.home state changes to detect when people come home or leave"""
        try:
            self.log(f"Home state changed: {old} -> {new}", level="INFO")
            
            # Get the persons who are home from the new state
            if new != "0":
                persons_home = self.get_state("zone.home", attribute="persons")
                if persons_home:
                    self.log(f"People home: {', '.join(persons_home)}", level="INFO")
                else:
                    self.log("Someone is home but no persons attribute available", level="INFO")
            else:
                self.log("No one is home", level="INFO")
            
            # Use unified decision tree; immediate when nobody home so lights clear quickly
            self.log("Home state change trigger: using unified decision tree", level="INFO")
            self._schedule_evaluation(immediate=(new == "0"))
            
        except Exception as e:
            self.log(f"Error handling home state change: {e}", level="ERROR")
    
    def _log_action(self, action, reason="", lights=""):
        """Unified logging for light actions"""
        if self.log_level == "quiet":
            return
        lights_str = f" ({lights})" if lights else ""
        reason_str = f": {reason}" if reason else ""
        self.log(f"lights {action} [family]{reason_str}{lights_str}", level="INFO")
        
    def _validate_and_recover_states(self, kwargs):
        """Validate and recover state tracking variables"""
        try:
            # Recover darkness state from committed classification
            try:
                current_dark = self._is_confirmed_dark()
            except Exception:
                current_dark = self._dark_flag if self._dark_flag is not None else True
            if self._dark_flag is None or self._last_darkness_state is None:
                self.log("Recovering darkness state tracking", level="INFO")
                self._dark_flag = current_dark
                self._last_darkness_state = current_dark
            
            # Recover family presence state
            current_presence = self._has_family_room_presence()
            if self._last_family_presence is None:
                self.log("Recovering family presence state tracking", level="INFO")
                self._last_family_presence = current_presence
            
            # Recover adjacent room presence state
            if not self._last_adjacent_room_presence:
                self.log("Recovering adjacent room presence state tracking", level="INFO")
                self._last_adjacent_room_presence = {}
            
            # Validate adjacent room states
            for room in self.adjacent_rooms:
                if room not in self._last_adjacent_room_presence:
                    self._last_adjacent_room_presence[room] = False
            
        except Exception as e:
            self.log(f"Error in state recovery: {e}", level="ERROR")
            # Reset all states to safe defaults
            self._dark_flag = None
            self._last_darkness_state = None
            self._last_family_presence = None
            self._last_adjacent_room_presence = {}

    def _log_darkness(self, indoor_lux, score, is_dark, triggered_by=""):
        """Deprecated: darkness logged by centralized calculator; keep minimal trace if used."""
        if self.log_level == "quiet":
            return
        trigger_str = f" ({triggered_by})" if triggered_by else ""
        result = "dark" if is_dark else "bright"  
        self.log(f"light_calc [family]: {indoor_lux:.0f}lx -> {score:.2f} -> {result}{trigger_str}", level="INFO") 

    def _on_manual_override_change(self, entity, attribute, old, new, kwargs):
        """Global mechanism: manual override toggle - pause/resume automatic lighting."""
        try:
            if new == "on":
                self.log("Manual override ON - automatic lighting paused for the family zone", level="INFO")
            elif new == "off":
                self.log("Manual override OFF - automatic lighting resumes (next evaluation, <=30s)", level="INFO")
        except Exception as e:
            self.log(f"Error in manual override handler: {e}", level="ERROR")
