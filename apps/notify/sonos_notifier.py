import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime, time # Added time for comparison
from urllib.parse import urlsplit

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
        self.claudia_sleep_mode_entity = self.args.get("claudia_sleep_mode_entity")

        # Speaker Configuration (Music Assistant room entities per TTS target group)
        self.tts_group_all = self._entity_list(self.args.get("tts_group_all"))  # everyone awake
        self.tts_group_family_rooms = self._entity_list(self.args.get("tts_group_family_rooms"))  # always used
        self.tts_group_kristine = self._entity_list(self.args.get("tts_group_kristine"))  # when Kristine is awake
        self.tts_group_ms = self._entity_list(self.args.get("tts_group_ms"))  # when Mikkel is awake
        self.tts_group_claudia = self._entity_list(self.args.get("tts_group_claudia"))  # when Claudia is awake

        # TTS Configuration
        self.default_chime_path = self.args.get("default_chime_path")
        self.tts_platform = self.args.get("tts_platform")
        self.audio_conversion = self.args.get("audio_conversion")
        self.announce_volume_boost = self.args.get("announce_volume_boost")
        self.announce_volume_cap = self.args.get("announce_volume_cap")
        self.clip_base_url = (self.args.get("clip_base_url") or "").rstrip("/")

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
                ("tts_group_claudia", self.tts_group_claudia),
            ] if not val
        ]
        if missing_groups:
            self.log(f"CRITICAL: Missing TTS group configuration keys: {', '.join(missing_groups)}. App will not function correctly.", level="ERROR")
            return
        if not self.kristine_sleep_mode_entity:
            self.log("WARN: Kristine's sleep mode entity not configured.")
        if not self.mikkel_sleep_mode_entity:
            self.log("WARN: Mikkel sleep mode entity not configured.")
        if not self.claudia_sleep_mode_entity:
            self.log("WARN: Claudia sleep mode entity not configured.")
        if not self.default_chime_path:
            self.log("WARN: 'default_chime_path' is not configured. Chime functionality might be affected if not provided in calls.")
        if not self.tts_platform:
            self.log("Using default TTS platform from chime_tts integration (no explicit tts_platform configured).", level="DEBUG")
        if self._volume_pct(self.announce_volume_cap) is None:
            self.log("WARN: 'announce_volume_cap' is missing or not a number. Announcements without a volume_level are not capped.", level="WARNING")

        self.log(f"{self.app_name} Initialized. Ready to send notifications.", level=self.log_level)
        self.log(
            f"TTS groups: all={self.tts_group_all}, family_rooms={self.tts_group_family_rooms}, "
            f"kristine={self.tts_group_kristine}, ms={self.tts_group_ms}, claudia={self.tts_group_claudia}",
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
        quiet hours (fail-safe); otherwise sleeping requires state == "on"."""
        if state in (None, "unknown", "unavailable"):
            return in_quiet_hours_now
        return state == "on"

    @staticmethod
    def _entity_list(entities):
        """An entity id or a list of them as a list without duplicates or empty values, order kept."""
        if entities is None:
            return []
        if isinstance(entities, str):
            entities = [entities]
        return list(dict.fromkeys(e for e in entities if e))

    @staticmethod
    def _say_url_payload(response):
        """chime_tts.say_url's own result dict inside AppDaemon's service-response envelope."""
        node = response
        for key in ("result", "response"):
            if isinstance(node, dict) and key in node:
                node = node[key]
        return node if isinstance(node, dict) else {}

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
            target_speakers (str | list, optional): Room entities to announce on; each plays on
                its native Sonos twin. Defaults to the sleep-mode based group selection.
            override_quiet_hours (bool, optional): Skip the quiet-hours check and announce
                anyway. For life-safety callers only. Defaults to False.
            volume_level (float, optional): Announcement volume (0.0-1.0) in every target room.
                When omitted each room announces announce_volume_boost above its own volume, at
                most announce_volume_cap.
            **kwargs: cache (default True), offset (default -300) and tts_platform for the
                chime_tts render.
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
            target_speakers = self._entity_list(target_speakers)
            self.log(f"Initial target speakers (override provided): {target_speakers}", level="DEBUG")
        else:
            # Unavailable/unknown -> assume sleeping only inside quiet hours (fail-safe).
            residents = (
                ("Mikkel", self.mikkel_sleep_mode_entity, self.tts_group_ms),
                ("Kristine", self.kristine_sleep_mode_entity, self.tts_group_kristine),
                ("Claudia", self.claudia_sleep_mode_entity, self.tts_group_claudia),
            )
            sleeping = []
            target_speakers = list(self.tts_group_family_rooms)
            for name, sleep_entity, private_rooms in residents:
                state = self.get_state(sleep_entity) if sleep_entity else "off"
                if self._resolve_sleeping(state, in_quiet_hours_now):
                    sleeping.append(name)
                else:
                    target_speakers.extend(private_rooms)
            if sleeping:
                self.log(f"Sleeping: {', '.join(sleeping)} -> family rooms + awake residents' rooms", level="INFO")
            else:
                target_speakers = self.tts_group_all
                self.log("No one sleeping -> all speakers group", level="DEBUG")

        target_speakers = self._entity_list(target_speakers)
        if not target_speakers:
            self.log(f"No target speakers determined. Notification for '{message}' will be skipped.", level="ERROR")
            return

        actual_chime_path = chime_path if chime_path is not None else self.default_chime_path

        if not actual_chime_path:
            self.log(f"No chime path provided or configured. Sending TTS for '{message}' to {target_speakers} without chime.", level="WARNING")

        cache = kwargs.get("cache", True)
        offset = kwargs.get("offset", -300)
        tts_platform = kwargs.get("tts_platform")
        if tts_platform is None:
            tts_platform = self.tts_platform

        self.log(
            f"Sending TTS: '{message}' with chime '{actual_chime_path}' to {target_speakers} (cache={cache}, offset={offset}, volume_level={volume_level}, audio_conversion={self.audio_conversion!r})",
            level=self.log_level,
        )

        url = self._render_announcement(message, actual_chime_path, cache, offset, tts_platform)
        if url is None:
            return
        self._play_announcement(message, url, target_speakers, volume_level)

    def _render_announcement(self, message, chime_path, cache, offset, tts_platform):
        """Render chime + speech with chime_tts and return the URL of the combined mp3, or None."""
        service_data = {"message": message, "cache": cache, "offset": offset}
        if chime_path:
            service_data["chime_path"] = chime_path
        if tts_platform:
            service_data["tts_platform"] = tts_platform
        if self.audio_conversion:
            service_data["audio_conversion"] = self.audio_conversion
        try:
            response = self.call_service("chime_tts/say_url", return_response=True, **service_data)
        except Exception as e:
            self.log(f"Error calling chime_tts/say_url for '{message}': {e}", level="ERROR")
            return None
        payload = self._say_url_payload(response)
        url = payload.get("url")
        if not payload.get("success") or not url:
            self.log(f"chime_tts/say_url produced no audio for '{message}': {response}", level="ERROR")
            return None
        self.log(f"Rendered '{message}' as {url} ({payload.get('duration')} s)", level="DEBUG")
        return self._clip_url(url)

    def _clip_url(self, url):
        """The rendered clip's URL on clip_base_url, keeping its path and query."""
        if not self.clip_base_url:
            return url
        parts = urlsplit(url)
        return f"{self.clip_base_url}{parts.path}" + (f"?{parts.query}" if parts.query else "")

    @staticmethod
    def _volume_pct(level):
        """A 0.0-1.0 volume in whole percent, or None when it is not a number."""
        try:
            return max(0, min(100, round(float(level) * 100)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _native_twin(room):
        """The native Sonos entity of a room entity (registry convention: <room>_2)."""
        return room if room.endswith("_2") else f"{room}_2"

    def _announce_groups(self, rooms, volume_level):
        """{announce volume in percent or None: native entities} for the rooms; one call each.

        A room announces announce_volume_boost above its own volume, at most
        announce_volume_cap. None means no volume is sent and the speaker plays the clip at its
        own volume, for a room whose volume cannot be read.
        """
        if volume_level is not None:
            return {self._volume_pct(volume_level): [self._native_twin(room) for room in rooms]}
        boost_pct = self._volume_pct(self.announce_volume_boost) or 0
        cap_pct = self._volume_pct(self.announce_volume_cap)
        groups = {}
        for room in rooms:
            pct = self._volume_pct(self.get_state(room, attribute="volume_level"))
            if pct is not None:
                pct += boost_pct
                if cap_pct is not None:
                    pct = min(pct, cap_pct)
            groups.setdefault(pct, []).append(self._native_twin(room))
        return groups

    def _play_announcement(self, message, url, rooms, volume_level):
        """Play the rendered clip as a Sonos audio clip on every room's native entity.

        The calls are dispatched without waiting on each other.
        Music Assistant is not used because it announces to the members of a Sonos group one
        after another.
        """
        for volume_pct, twins in self._announce_groups(rooms, volume_level).items():
            extra = {} if volume_pct is None else {"volume": volume_pct}
            try:
                self.call_service(
                    "media_player/play_media",
                    entity_id=twins,
                    media_content_id=url,
                    media_content_type="music",
                    announce=True,
                    extra=extra,
                    callback=lambda result, twins=twins, volume_pct=volume_pct: self._log_announcement_result(
                        message, twins, volume_pct, result
                    ),
                )
            except Exception as e:
                self.log(f"Error calling media_player/play_media for {twins}: {e}", level="ERROR")

    def _log_announcement_result(self, message, rooms, volume_pct, result):
        volume = "own volume" if volume_pct is None else f"announce volume {volume_pct}%"
        if isinstance(result, dict) and result.get("success"):
            self.log(f"Announced '{message}' on {rooms} ({volume})", level="INFO")
        else:
            self.log(f"Announcing '{message}' on {rooms} ({volume}) failed: {result}", level="ERROR")

    # Example of how another app might call this notifier:
    # notifier = self.get_app("SonosNotifier")  # Assuming 'SonosNotifier' is the name in apps.yaml
    # notifier.notify(message="Test message from another app", chime_path="/config/www/chimes/another_chime.mp3")
