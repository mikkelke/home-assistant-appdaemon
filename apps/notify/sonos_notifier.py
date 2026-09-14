import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime, time # Added time for comparison

# App to handle Sonos TTS notifications with sleep mode considerations
class SonosNotifier(hass.Hass):

    def initialize(self):
        """Initialize the SonosNotifier app."""
        # General App Settings
        self.app_name = self.args.get("module", "SonosNotifier") # Default to module name if not specified
        self.log_level = self.args.get("log_level", "INFO") # Default log level

        # Sleep Mode Entities
        self.kristine_sleep_mode_entity = self.args.get("kristine_sleep_mode_entity")
        self.mikkel_sleep_mode_entity = self.args.get("mikkel_sleep_mode_entity")

        # Speaker Configuration (group entities for TTS routing)
        self.tts_group_all = self.args.get("tts_group_all")  # everyone awake
        self.tts_group_family_rooms = self.args.get("tts_group_family_rooms")  # always used
        self.tts_group_kristine = self.args.get("tts_group_kristine")  # when only Mikkel sleeps
        self.tts_group_ms = self.args.get("tts_group_ms")  # when only Kristine sleeps

        # TTS Configuration
        self.default_chime_path = self.args.get("default_chime_path")
        self.tts_platform = self.args.get("tts_platform")

        # Time Constraints using input_datetime entities
        self.time_constraints_enabled = False
        self.quiet_hours_end_entity = self.args.get("quiet_hours_end_entity")
        self.quiet_hours_start_entity = self.args.get("quiet_hours_start_entity")

        # Fallback defaults for when the helpers above are unavailable/unparseable, so an
        # outage fails SAFE (quiet) instead of open (loud, house-wide) - see notify().
        self._default_quiet_hours_start = self._parse_time_str(self.args.get("default_quiet_hours_start")) or time(22, 0)
        self._default_quiet_hours_end = self._parse_time_str(self.args.get("default_quiet_hours_end")) or time(7, 0)
        self._quiet_hours_fallback_active = False

        if self.quiet_hours_end_entity and self.quiet_hours_start_entity:
            self.time_constraints_enabled = True
            self.log(f"Time constraints enabled. Using {self.quiet_hours_end_entity} and {self.quiet_hours_start_entity}.", level="INFO")
            # We will fetch and parse the time from these entities in the notify() method
        else:
            self.log("WARN: 'quiet_hours_end_entity' and/or 'quiet_hours_start_entity' not defined in YAML. Time constraints will be DISABLED. Notifications will be allowed at all times.", level="WARNING")

        # Validate essential configuration (excluding time constraints as they can be disabled)
        missing_groups = [
            name for name, val in [
                ("tts_group_all", self.tts_group_all),
                ("tts_group_family_rooms", self.tts_group_family_rooms),
                ("tts_group_kristine", self.tts_group_kristine),
                ("tts_group_ms", self.tts_group_ms),
            ] if not val
        ]
        if missing_groups:
            self.log(f"CRITICAL: Missing TTS group configuration keys: {', '.join(missing_groups)}. App will not function correctly.", level="ERROR")
            return
        if not self.kristine_sleep_mode_entity:
            self.log("WARN: Kristine's sleep mode entity not configured.")
        if not self.mikkel_sleep_mode_entity:
            self.log("WARN: Mikkel sleep mode entity not configured.")
        if not self.default_chime_path:
            self.log("WARN: 'default_chime_path' is not configured. Chime functionality might be affected if not provided in calls.")
        if not self.tts_platform:
            self.log("Using default TTS platform from chime_tts integration (no explicit tts_platform configured).", level="DEBUG")

        self.log(f"{self.app_name} Initialized. Ready to send notifications.", level=self.log_level)
        self.log(
            f"TTS groups: all={self.tts_group_all}, family_rooms={self.tts_group_family_rooms}, "
            f"kristine={self.tts_group_kristine}, ms={self.tts_group_ms}",
            level="DEBUG",
        )
        self.log(f"Default Chime Path: {self.default_chime_path}", level="DEBUG")

    @staticmethod
    def _parse_time_str(state_str):
        """Parse an HH:MM or HH:MM:SS string into a time object, or None if invalid."""
        if not state_str:
            return None
        try:
            if len(state_str) == 8: # HH:MM:SS
                return datetime.strptime(state_str, "%H:%M:%S").time()
            elif len(state_str) == 5: # HH:MM
                return datetime.strptime(state_str, "%H:%M").time()
        except ValueError:
            return None
        return None

    def _get_time_from_entity(self, entity_id):
        """Safely gets and parses time from an input_datetime entity state."""
        state_str = self.get_state(entity_id)
        if state_str is None or state_str in ["unknown", "unavailable"]:
            self.log(f"ERROR: Entity {entity_id} for time constraint is unavailable or state is unknown ('{state_str}').", level="ERROR")
            return None
        parsed = self._parse_time_str(state_str)
        if parsed is None:
            self.log(f"ERROR: Invalid/unparseable time format '{state_str}' from entity {entity_id}. Expected HH:MM:SS or HH:MM.", level="ERROR")
        return parsed

    @staticmethod
    def _is_quiet_hours_now(current_time_obj, quiet_hours_start, quiet_hours_end):
        """True when current_time_obj falls inside the [start, end) quiet-hours window."""
        if quiet_hours_end < quiet_hours_start:
            return not (current_time_obj >= quiet_hours_end and current_time_obj < quiet_hours_start)
        return current_time_obj >= quiet_hours_start or current_time_obj < quiet_hours_end

    @staticmethod
    def _resolve_sleeping(state, in_quiet_hours_now):
        """An unavailable/unknown sleep-mode entity is assumed sleeping only while inside
        quiet hours (fail-safe); outside quiet hours this matches prior behaviour."""
        if state in (None, "unknown", "unavailable"):
            return in_quiet_hours_now
        return state == "on"

    def _normalize_sonos_entity(self, entity_id):
        """Normalize Sonos entity IDs from legacy cloud suffix to direct entity.

        Example: media_player.living_room_2 -> media_player.living_room
        """
        try:
            if isinstance(entity_id, str) and entity_id.startswith("media_player.") and entity_id.endswith("_2"):
                return entity_id[:-2]
        except Exception:
            pass
        return entity_id

    def _normalize_target_list(self, entities):
        """Return a new list with normalized Sonos entity IDs."""
        if entities is None:
            return []
        try:
            if isinstance(entities, str):
                entities = [entities]
            return [self._normalize_sonos_entity(e) for e in list(entities)]
        except Exception:
            return list(entities) if isinstance(entities, list) else []

    def notify(
        self,
        message,
        chime_path=None,
        target_speakers=None,
        override_quiet_hours: bool = False,
        volume_level: float | None = None,
        **kwargs,
    ):
        """
        Sends a TTS notification to Sonos speakers, respecting configured sleep modes and time constraints.

        Args:
            message (str): The message to be spoken.
            chime_path (str, optional): Path to the chime sound file.
                                      Defaults to 'default_chime_path' from config.
            override_quiet_hours (bool, optional): Skip the quiet-hours check and announce
                anyway. For life-safety callers only. Defaults to False.
            volume_level (float, optional): Passed through to chime_tts/say when given.
            **kwargs: Placeholder for future arguments.
        """
        if not message:
            self.log("Notification attempt with no message. Aborting.", level="WARNING")
            return

        # TEMPORARILY BYPASS TIME CONSTRAINTS FOR TESTING
        # if self.time_constraints_enabled and True: # Set to False to enable time constraints, True to bypass
        #     self.log("WARNING: TIME CONSTRAINT CHECK IS CURRENTLY BYPASSED IN CODE FOR TESTING!", level="WARNING")
        #     pass # Bypass the actual time check logic below
        # elif self.time_constraints_enabled:
        # END TEMPORARY BYPASS
        in_quiet_hours_now = False
        if self.time_constraints_enabled:
            raw_quiet_hours_end = self._get_time_from_entity(self.quiet_hours_end_entity)
            raw_quiet_hours_start = self._get_time_from_entity(self.quiet_hours_start_entity)
            helpers_ok = raw_quiet_hours_end is not None and raw_quiet_hours_start is not None

            if not helpers_ok and not self._quiet_hours_fallback_active:
                self._quiet_hours_fallback_active = True
                self.log(
                    "Quiet-hours helper(s) unavailable/unparseable. Falling back to configured defaults "
                    f"({self._default_quiet_hours_start.strftime('%H:%M')}-{self._default_quiet_hours_end.strftime('%H:%M')}) "
                    "until helpers recover.",
                    level="WARNING",
                )
            elif helpers_ok and self._quiet_hours_fallback_active:
                self._quiet_hours_fallback_active = False
                self.log("Quiet-hours helpers are readable again. Resuming normal quiet-hours checks.", level="INFO")

            # Fall back per-entity: an outage on one helper doesn't have to blind the other.
            quiet_hours_end = raw_quiet_hours_end if raw_quiet_hours_end is not None else self._default_quiet_hours_end
            quiet_hours_start = raw_quiet_hours_start if raw_quiet_hours_start is not None else self._default_quiet_hours_start
            current_time_obj = self.datetime().time()
            in_quiet_hours_now = self._is_quiet_hours_now(current_time_obj, quiet_hours_start, quiet_hours_end)

        if override_quiet_hours:
            self.log(f"override_quiet_hours=True: bypassing quiet-hours check for '{message}'.", level="WARNING")
        elif self.time_constraints_enabled:
            if in_quiet_hours_now:
                self.log(f"Notification for '{message}' skipped. Current time {current_time_obj.strftime('%H:%M:%S')} is within quiet hours ({quiet_hours_start.strftime('%H:%M:%S')} - {quiet_hours_end.strftime('%H:%M:%S')}).", level="INFO")
                return
        else:
            self.log("Time constraints are disabled (YAML config missing). Proceeding with notification.", level="DEBUG")

        # Determine target speakers (override wins; otherwise use grouped strategy)
        if target_speakers is not None:
            target_speakers = self._normalize_target_list(target_speakers)
            self.log(f"Initial target speakers (override provided): {target_speakers}", level="DEBUG")
        else:
            kristine_sleep_state = self.get_state(self.kristine_sleep_mode_entity) if self.kristine_sleep_mode_entity else "off"
            mikkel_sleep_state = self.get_state(self.mikkel_sleep_mode_entity) if self.mikkel_sleep_mode_entity else "off"

            # Unavailable/unknown -> assume sleeping only inside quiet hours (fail-safe).
            kristine_sleeping = self._resolve_sleeping(kristine_sleep_state, in_quiet_hours_now)
            mikkel_sleeping = self._resolve_sleeping(mikkel_sleep_state, in_quiet_hours_now)
            everyone_sleeping = kristine_sleeping and mikkel_sleeping

            if everyone_sleeping:
                target_speakers = [self.tts_group_family_rooms]
                self.log("All sleeping -> using family rooms group", level="INFO")
            elif kristine_sleeping:
                target_speakers = [self.tts_group_family_rooms, self.tts_group_ms]
                self.log("Kristine sleeping -> family rooms + Mikkel group", level="INFO")
            elif mikkel_sleeping:
                target_speakers = [self.tts_group_family_rooms, self.tts_group_kristine]
                self.log("Mikkel sleeping -> family rooms + Kristine group", level="INFO")
            else:
                target_speakers = [self.tts_group_all]
                self.log("No one sleeping -> all speakers group", level="DEBUG")

        # Remove duplicates/None and normalize
        target_speakers = self._normalize_target_list([t for t in target_speakers if t])
        if not target_speakers:
            self.log(f"No target speakers determined. Notification for '{message}' will be skipped.", level="ERROR")
            return

        actual_chime_path = chime_path if chime_path is not None else self.default_chime_path

        if not actual_chime_path:
            self.log(f"No chime path provided or configured. Sending TTS for '{message}' to {target_speakers} without chime.", level="WARNING")
            # Potentially call a different service or the same service without chime_path if supported
            # For now, we assume chime_tts.say requires it or handles its absence gracefully.

        # Extract optional service parameters (defaults aligned with central policy)
        announce = bool(kwargs.get("announce", True))
        fade_audio = bool(kwargs.get("fade_audio", True))
        cache = kwargs.get("cache", True)  # cache re-enabled: recreating the piper container (2.2.2-ls113) cleared chime_tts/piper stuck state; tested 8/8 clean. If silent-doorbell ("Unable to generate local audio filepath") recurs -> restart piper container, or set this False (backup .bak_cachefalse).
        # Keep offset support; default to -300 to start TTS sooner after chime
        offset = kwargs.get("offset", -300)

        self.log(
            f"Sending TTS: '{message}' with chime '{actual_chime_path}' to {target_speakers} (announce={announce}, fade_audio={fade_audio}, cache={cache}, offset={offset})",
            level=self.log_level,
        )

        # Original service call logic:
        # 'target_speakers' is already filtered and checked for emptiness before the "Sending TTS..." log.

        self.log(f"Preparing single API call to chime_tts/say for target(s): {target_speakers}", level="DEBUG")
        
        try:
            # Build service data and include tts_platform only if explicitly configured
            tts_platform_override = kwargs.get("tts_platform")
            tts_platform_to_use = tts_platform_override if tts_platform_override is not None else self.tts_platform

            service_data = {
                "entity_id": target_speakers,
                "message": message,
                "chime_path": actual_chime_path,
                "cache": cache,
                "announce": announce,
                "fade_audio": fade_audio,
                "offset": offset,
            }
            if tts_platform_to_use:
                service_data["tts_platform"] = tts_platform_to_use
            if volume_level is not None:
                service_data["volume_level"] = volume_level

            self.call_service(
                "chime_tts/say",
                **service_data
            )
            self.log(f"Successfully initiated chime_tts/say service call for target(s): {target_speakers} with message: '{message}'", level="INFO")
        except Exception as e:
            self.log(f"Error calling chime_tts/say service for target(s) {target_speakers}: {e}", level="ERROR")
            # Detailed log for error
            error_details = {
                "target_entity_id": target_speakers,
                "message": message,
                "chime_path": actual_chime_path,
                "tts_platform": kwargs.get("tts_platform", self.tts_platform),
                "cache": True
            }
            self.log(f"Service call failed. Details: {error_details}", level="DEBUG")

    # Example of how another app might call this notifier:
    # notifier = self.get_app("SonosNotifier")  # Assuming 'SonosNotifier' is the name in apps.yaml
    # notifier.notify(message="Test message from another app", chime_path="/config/www/chimes/another_chime.mp3")
