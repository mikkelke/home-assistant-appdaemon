# /conf/apps/sonos/follow_me.py
import appdaemon.plugins.hass.hassapi as hass   # type: ignore

class SonosFollowMe(hass.Hass):
    """
    Follow-me rules engine: mute/unmute speakers by room presence.
    • Follow-me runs whenever relevant (grouped or solo), with rules: living_room, kristines_room, and sometimes rooftop do not activate follow-me when solo.
    • desired_mute = not present (unmute if someone in room, mute if not). Only applies to speakers that are actually playing.
    • Kitchen uses kitchen OR hallway presence; rooftop undocked is always present, docked follows living_room.
    • During reset, mute/unmute is paused; after reset, state is re-synced.
    """

    def initialize(self):
        self.room_map    = self.args["room_presence_map"]   # room -> binary_sensor (may be a HA template combining inputs)
        self.speaker_map = self.args["speaker_room_map"]   # speaker_entity -> room name
        self.follow_flag = self.args["follow_me_flag"]
        # Support both YAML key names (throttle_seconds/settle_seconds and min_update_interval)
        self.throttle    = float(self.args.get("throttle_seconds") or self.args.get("min_update_interval", 2))
        self.settle      = float(self.args.get("settle_seconds", 2))
        self.reset_resume_delay = float(self.args.get("reset_resume_delay", 2.5))
        self.special     = self.args.get("special_conditions", {})
        self.entity_friendly_name_map = self.args.get("entity_friendly_name_map", {})
        self._friendly_to_entity_map = {v: k for k, v in self.entity_friendly_name_map.items()}
        self._last       = {}      # now a dict: room -> last timestamp
        self._pending_changes = {}
        self._pending_handle = None
        
        # Track reset state to coordinate with state_reset app
        self._reset_in_progress = False
        self._reset_resume_handle = None
        self._fm_reset_generation = 0
        
        # Track last non-zero volume per speaker for AirPlay restore
        self._last_nonzero_volume = {}
        
        # Coordinator cache for _get_current_coordinator() optimization
        # Cache with 1-second TTL, invalidated on group changes
        self._cached_coordinator = None
        self._cached_coordinator_time = None
        self._coordinator_cache_ttl = 1.0  # 1 second TTL
        # AirPlay fallback volume comes from SonosStateReset (single place for reset/default volume)
        
        # Ensure media_player.rooftop maps to the "rooftop" room name for entity_id -> room_name lookups
        # This is used, for example, in _check_settled when iterating group members.
        if "media_player.rooftop" not in self.speaker_map:
            self.speaker_map["media_player.rooftop"] = "rooftop"
        
        # Create an inverse map for efficient room_name -> speaker_entity_id lookup
        # This map will be used in _presence_changed.
        self._room_to_speaker_entity_map = {room_name: entity_id for entity_id, room_name in self.speaker_map.items()}
        
        # Register presence sensors with rooms directly in callback
        for room, sensor in self.room_map.items():
            if room == "rooftop_charging":
                continue
            self.listen_state(self._presence_changed, sensor, room=room)

        # Listen to volume changes for all speakers to remember last non-zero level
        for sp in self.args.get("all_speakers", []):
            self.listen_state(self._on_volume_level_change, sp, attribute="volume_level")
            # Listen for mute state changes to preserve mute when unmuted externally
            self.listen_state(self._on_mute_state_change, sp, attribute="is_volume_muted")
            # Listen for state changes to unmute when speakers stop playing
            self.listen_state(self._on_speaker_state_change, sp)
            # Listen for group changes to unmute when speakers become solo
            # Also invalidate coordinator cache on group changes
            self.listen_state(self._on_speaker_group_change, sp, attribute="group_members")
            # Invalidate coordinator cache when speaker state changes (could affect coordinator)
            self.listen_state(self._invalidate_coordinator_cache, sp)

        # NOTE: Follow_me toggle is no longer used - automatic detection is used instead
        # The follow_flag is kept for backward compatibility but not actively monitored

        # Listen for rooftop docking/charging state changes
        if "rooftop_charging" in self.room_map:
            self.listen_state(self._on_rooftop_charging_change, 
                             self.room_map["rooftop_charging"])
            
        # Special listener for living room presence affecting rooftop speaker
        if "living_room" in self.room_map and self.special.get("rooftop_with_living_room"):
            self.listen_state(self._on_living_room_presence_change,
                             self.room_map["living_room"])

        # NEW CODE: Listen for direct group update events from GroupManager
        self.listen_event(self._on_sonos_group_update, "sonos_group_update")
        
        # Listen for reset events to coordinate with state_reset app
        self.listen_event(self._on_reset_started, "sonos_reset_started")
        self.listen_event(self._on_reset_completed, "sonos_reset_completed")

        # On startup, check and unmute any solo speakers that aren't playing
        self.run_in(self._check_and_unmute_stale_speakers, 5)

        self.log("Scenario: SonosFollowMe_loaded", level="INFO")

    def _safe_cancel_timer(self, timer_handle):
        if timer_handle is None:
            return False
        try:
            if self.timer_running(timer_handle):
                self.cancel_timer(timer_handle)
                return True
            return False
        except Exception as e:
            self.log(f"Error checking/cancelling timer: {e}", level="DEBUG")
            return False

    def _on_volume_level_change(self, entity, attribute, old, new, kwargs):
        """Track last non-zero volume per speaker for AirPlay restore. When AirPlay sets volume to 0, restore it."""
        try:
            if new is None:
                return
            vol = float(new)
            
            # Save restore volume for every speaker when using AirPlay (so we can unmute + set volume when people are present)
            if self._should_follow_me_be_active() and vol > 0.0:
                self._last_nonzero_volume[entity] = vol
            
            # When AirPlay sets volume to 0 on any speaker, restore so user hears sound when there is people
            if vol <= 0.0 and self._should_follow_me_be_active():
                self.run_in(lambda _: self._restore_volume_if_airplay(entity), 0.2)
            # Preserve mute state when volume changes (handled by _on_mute_state_change with debounce)
            # Volume changes from external sources (Spotify Connect, Music Assistant, etc.) can unmute speakers
            # We use a small delay to let the mute state change propagate first
            self.run_in(lambda _: self._preserve_mute_state_if_needed(entity), 0.3)
        except Exception:
            # Ignore parse errors silently to avoid noisy logs
            pass
    
    def _on_mute_state_change(self, entity, attribute, old, new, kwargs):
        """Handle mute state changes - preserve mute if speaker should be muted based on presence."""
        try:
            # Only process if follow_me is active
            if not self._should_follow_me_be_active():
                return
            
            # If speaker was just unmuted, check if it should remain muted
            if self._parse_muted(old) and not self._parse_muted(new):
                # Use a small delay to avoid race conditions with volume changes
                self.run_in(lambda _: self._preserve_mute_state_if_needed(entity), 0.2)
        except Exception:
            # Ignore parse errors silently to avoid noisy logs
            pass
    
    def _preserve_mute_state_if_needed(self, entity):
        """Check if speaker should be muted and re-apply mute if needed.
        
        This handles cases where external volume/mute changes (Spotify Connect, Music Assistant, etc.)
        unmute speakers that should remain muted based on presence.
        """
        try:
            # Only process if follow_me is active
            if not self._should_follow_me_be_active():
                return
            
            # Check if this speaker is part of a group (follow_me only applies to grouped speakers)
            members = (self.get_state(entity, attribute="sonos_group") or
                      self.get_state(entity, attribute="group_members") or [])
            is_grouped = len(members) > 1
            
            # Only enforce mute state for grouped speakers
            if not is_grouped:
                return
            
            # Find which room this speaker is in
            speaker_room = self.speaker_map.get(entity)
            if not speaker_room:
                return
            
            # Check if this room is excluded from follow_me
            if self._is_room_excluded_from_follow_me(speaker_room):
                return
            
            # Never re-mute idle/paused speakers (follow-me mute only applies while playing)
            if not self._is_speaker_playing(entity):
                return
            
            # Check presence in this room
            present = self._is_present(speaker_room, entity)
            if present is None:
                return
            
            # Speaker should be muted if no presence
            desired_mute = not present
            if desired_mute and not self._is_muted(entity):
                self.log(f"Scenario: preserve_mute_state -> {entity} was unmuted externally but should be muted (no presence in {speaker_room}), re-applying mute", level="INFO")
                self.call_service("media_player/volume_mute",
                                entity_id=entity,
                                is_volume_muted=True)
        except Exception:
            # Ignore errors silently to avoid noisy logs
            pass

    def _restore_volume_if_airplay(self, entity_id, delay_seconds=0):
        """Restore volume only when volume is 0: unmute and set to saved or default (covers AirPlay; no reason to leave at 0).
        When delay_seconds > 0, runs after that delay to catch late volume=0 (e.g. after AirPlay unmute)."""
        if delay_seconds > 0:
            self.run_in(
                lambda _: self._restore_volume_if_airplay(entity_id, delay_seconds=0),
                delay_seconds
            )
            return
        try:
            if not self._should_follow_me_be_active():
                self.log(f"Scenario: volume_restore_skip -> {entity_id} follow_me off", level="DEBUG")
                return
            curr = self.get_state(entity_id, attribute="volume_level")
            curr_f = float(curr) if curr is not None else 0.0
            if curr_f > 0.0:
                self.log(f"Scenario: volume_restore_skip -> {entity_id} volume already {curr_f}", level="DEBUG")
                return
            # Volume is 0: unmute and set volume (AirPlay often does this; no reason to leave at 0)
            self.call_service("media_player/volume_mute", entity_id=entity_id, is_volume_muted=False)
            # No stored volume or stored volume is 0 -> use default (state_reset). Never set volume to 0.
            last = self._last_nonzero_volume.get(entity_id)
            default_level = self._get_reset_volume_for_speaker(entity_id)
            level = last if (last is not None and last > 0.0) else default_level
            level = max(level, default_level, 0.01)
            self.call_service("media_player/volume_set",
                              entity_id=entity_id,
                              volume_level=level)
            if last is None or last <= 0.0:
                self.log(f"Scenario: volume_restore -> {entity_id} vol 0: unmute + set volume {level} (default)", level="INFO")
            else:
                self.log(f"Scenario: volume_restore -> {entity_id} vol 0: unmute + restore volume {level}", level="INFO")
        except Exception as e:
            self.log(f"Scenario: volume_restore_error -> {entity_id} {e}", level="DEBUG")
            pass

    def _get_reset_volume_for_speaker(self, entity_id):
        """Return the volume level state_reset would use for this speaker (single source of truth)."""
        try:
            reset_app = self.get_app("SonosStateReset")
            volumes = getattr(reset_app, "speaker_volumes", None) or {}
            default = float(getattr(reset_app, "default_volume", 0.18))
            return float(volumes.get(entity_id, default))
        except Exception:
            return 0.18

    def _is_present(self, room, speaker):
        """Check if a room has presence.
        Returns True/False when determinable, or None when state is unknown/unavailable/missing.
        """
        # Special case for rooftop
        if room == "rooftop" or speaker.endswith("rooftop"):
            # Manual override: while the keep-speaker-on toggle is set, keep the rooftop
            # speaker playing regardless of dock/charging or living-room motion (e.g. charging
            # on the terrace with no indoor motion). Turn the toggle off to resume follow-me.
            if self.get_state("input_boolean.rooftop_keep_speaker_on") == "on":
                self.log(f"Scenario: presence_check -> rooftop keep_speaker_on ON, forcing presence:True", level="INFO")
                return True

            # Check if rooftop is charging/docked
            charging = self.get_state(self.room_map["rooftop_charging"])
            is_charging = charging in ["on", "home"]
            
            # If rooftop is NOT charging/docked, always report presence as TRUE
            # This ensures it's always unmuted when not in the dock
            if not is_charging:
                self.log(f"Scenario: presence_check -> rooftop NOT charging/docked, forcing presence:True", level="INFO")
                return True
                
            # If it IS charging/docked, follow living room presence
            living_room = self.get_state(self.room_map["living_room"])
            if living_room is None or str(living_room).lower() in ["unknown", "unavailable"]:
                self.log(f"Scenario: presence_check -> rooftop docked, living_room:{living_room} (ignoring)", level="WARNING")
                return None
            result = str(living_room).lower() in ["on", "home"]
            self.log(f"Scenario: presence_check -> rooftop docked, following_living_room: {result} (living_room: {living_room})", level="INFO")
            return result
            
        # Special case for kitchen - check both kitchen and hallway sensors
        if room == "kitchen" and self.special.get("kitchen_or_hallway"):
            kitchen_state = self.get_state(self.room_map["kitchen"])
            hallway_state = self.get_state(self.room_map["hallway"])
            present_values = ["on", "home", "detected", "present", "occupied", "true", "yes"]
            # Determine known/unknown
            k_raw = None if kitchen_state is None else str(kitchen_state).lower()
            h_raw = None if hallway_state is None else str(hallway_state).lower()
            k_unknown = (k_raw is None) or (k_raw in ["unknown", "unavailable"]) 
            h_unknown = (h_raw is None) or (h_raw in ["unknown", "unavailable"]) 
            k_present = (not k_unknown) and (k_raw in present_values)
            h_present = (not h_unknown) and (h_raw in present_values)
            # If both are unknown, we cannot decide
            if k_unknown and h_unknown:
                self.log(f"Scenario: kitchen_presence -> both_unknown (kitchen:{kitchen_state} hallway:{hallway_state}), ignoring", level="WARNING")
                return None
            # Otherwise, presence is true if any known sensor is present
            result = k_present or h_present
            self.log(f"Scenario: kitchen_presence -> kitchen:{kitchen_state} hallway:{hallway_state} result:{result}", level="INFO")
            return result
            
        # Handle regular rooms (entity may be a HA template that already combines motion + presence, etc.)
        sensor = self.room_map.get(room)
        if not sensor:
            self.log(f"Scenario: presence_check -> missing_sensor for room {room}", level="WARNING")
            return None

        state = self.get_state(sensor)

        if state is None:
            self.log(f"Scenario: presence_check -> {room} sensor:{sensor} returned None (ignoring)", level="WARNING")
            return None

        if state == "unavailable" or state == "unknown":
            self.log(f"Scenario: presence_check -> {room} sensor:{sensor} is {state} (ignoring)", level="WARNING")
            return None

        result = state.lower() in ["on", "home", "detected", "present", "occupied", "true", "yes"]
        self.log(f"Scenario: presence_check -> {room} result:{result} raw_state:{state} sensor:{sensor}", level="INFO")
        return result

    def _trigger_follow_me_sync(self, reason="condition_change"):
        """Trigger a follow_me sync when conditions change.
        
        This method is called when follow_me conditions might have changed
        (e.g., speakers grouped/ungrouped, all speakers start/stop playing).
        """
        if not self._should_follow_me_be_active():
            return
        
        self.log(f"Scenario: follow_me_sync_triggered -> {reason}, syncing all rooms", level="INFO")
        # wait for both this app's settle and GroupManager's settle, plus extra time for group state
        delay = self.settle + float(self.args.get("group_manager_settle", self.settle)) + 2
        # Avoid AppDaemon "Invalid callback handle" warnings by only cancelling running timers
        handle_to_cancel = self._pending_handle
        self._pending_handle = None
        try:
            if handle_to_cancel and self.timer_running(handle_to_cancel):
                self.cancel_timer(handle_to_cancel)
        except Exception:
            # Be defensive: timer_running/cancel_timer can fail if handle is malformed
            pass
        self._pending_handle = self.run_in(self._check_settled, delay)

    def _on_speaker_state_change(self, entity, attribute, old, new, kwargs):
        """Handle speaker state changes - unmute when not playing; sync follow_me when active."""
        was_playing = old == "playing"
        is_now_playing = new == "playing"

        # Strict rule: follow-me mute is only meaningful while playing. Never leave idle/paused/etc. muted.
        # Must run even when follow_me is "inactive" (no coordinator after playback stops - old code returned
        # early and never unmuted). Also applies while still grouped (old code required is_solo).
        if new is not None:
            ns = str(new).lower()
            if ns not in ("unavailable", "unknown") and not is_now_playing:
                if self._is_muted(entity) and not self._reset_in_progress:
                    self.log(
                        f"Scenario: unmute_not_playing -> {entity} state={new}, clearing mute (idle/pause must not stay muted)",
                        level="INFO",
                    )
                    self.call_service(
                        "media_player/volume_mute",
                        entity_id=entity,
                        is_volume_muted=False,
                    )

        if not self._should_follow_me_be_active():
            return

        # Trigger sync if state change might affect follow_me conditions (e.g., all speakers playing)
        if was_playing != is_now_playing:
            self._trigger_follow_me_sync(f"speaker_state_change_{entity}")

    def _on_speaker_group_change(self, entity, attribute, old, new, kwargs):
        """Handle group membership changes - unmute non-playing when becoming solo; sync follow_me when active."""
        old_members = old or []
        new_members = new or []

        was_grouped = len(old_members) > 1
        now_solo = len(new_members) <= 1

        # Same strict rule: if follow_me is inactive, we still unmute when leaving group while not playing
        # (GroupManager also unmutes on solo; this covers edge cases.)
        if was_grouped and now_solo:
            state = self.get_state(entity)
            is_playing = state == "playing"
            if not is_playing and self._is_muted(entity) and not self._reset_in_progress:
                self.log(
                    f"Scenario: speaker_became_solo_not_playing -> {entity} solo, not playing (state: {state}), unmuting",
                    level="INFO",
                )
                self.call_service(
                    "media_player/volume_mute",
                    entity_id=entity,
                    is_volume_muted=False,
                )

        if not self._should_follow_me_be_active():
            return

        # Trigger sync if group change might affect follow_me conditions (grouped <-> solo)
        if was_grouped != (not now_solo):
            self._trigger_follow_me_sync(f"speaker_group_change_{entity}")

    def _check_and_unmute_stale_speakers(self, kwargs):
        """On startup, unmute any speaker that is not playing but still muted (solo or grouped)."""
        self.log("Scenario: startup_check -> checking for stale muted speakers", level="INFO")

        for sp in self.args.get("all_speakers", []):
            state = self.get_state(sp)
            is_playing = state == "playing"
            if not is_playing and self._is_muted(sp):
                self.log(
                    f"Scenario: startup_unmute_stale -> {sp} not playing (state: {state}) but muted - unmuting",
                    level="INFO",
                )
                self.call_service(
                    "media_player/volume_mute",
                    entity_id=sp,
                    is_volume_muted=False,
                )

    def _presence_changed(self, entity, attribute, old, new, kwargs):
        """Handle presence sensor state changes."""
        # Get room directly from callback kwargs
        room = kwargs.get('room')
        now = self.datetime().timestamp()
        
        if not room:
            self.log(f"Scenario: unknown_presence_sensor -> '{entity}'", level="WARNING")
            return

        # Hallway has no speaker; when kitchen_or_hallway is enabled, re-evaluate kitchen mute.
        if room == "hallway" and self.special.get("kitchen_or_hallway", False):
            self.log(
                f"Scenario: presence_change_hallway -> hallway {old} to {new}, re-evaluating kitchen",
                level="INFO",
            )
            room = "kitchen"
        
        # Check if this room is excluded from follow_me
        if self._is_room_excluded_from_follow_me(room):
            self.log(f"Scenario: presence_change_room_excluded -> {room} is excluded from follow_me, skipping", level="DEBUG")
            return
        
        # OPTIMIZATION: Cache frequently accessed states at start of callback
        follow_me_enabled = self._should_follow_me_be_active()
        
        # Log the presence change with appropriate level based on follow_me state
        if follow_me_enabled:
            self.log(f"Scenario: presence_change -> {room} changed from {old} to {new}", level="INFO")
        else:
            # Only log at DEBUG level when follow_me is off to reduce noise
            self.log(f"Scenario: presence_change -> {room} changed from {old} to {new}", level="DEBUG")
            
        # Throttle duplicate callbacks; never drop a transition that indicates presence (fixes quick off->on on template).
        present_edge_values = ["on", "home", "detected", "present", "occupied", "true", "yes"]
        within_throttle = now - self._last.get(room, 0) < self.throttle
        new_present = isinstance(new, str) and new.lower() in present_edge_values
        if within_throttle and not new_present:
            return
        self._last[room] = now

        # Skip if follow_me is off
        if not follow_me_enabled:
            self.log(f"Scenario: presence_{room}_{new} -> follow_me OFF, skipping", level="DEBUG")
            return

        # CRITICAL: Find the actual media_player entity for this room using direct mapping
        speaker_entity = None
        
        # Handle rooftop special case
        if room == "rooftop" or room == "rooftop_charging":
            speaker_entity = "media_player.rooftop"
        else:
            # Use the pre-calculated direct map for room_name to speaker_entity
            speaker_entity = self._room_to_speaker_entity_map.get(room)
            if not speaker_entity:
                self.log(f"Scenario: speaker_not_mapped -> No speaker entity is mapped to room '{room}' in speaker_room_map.", level="DEBUG")
                return
            
        # Validate entity exists
        if self.get_state(speaker_entity) is None:
            self.log(f"Scenario: invalid_speaker_entity -> Mapped speaker '{speaker_entity}' for room '{room}' does not exist in Home Assistant.", level="DEBUG")
            return
        
        # Debug log the speaker entity
        self.log(f"Scenario: found_speaker -> room:{room} speaker:{speaker_entity}", level="DEBUG")

        # Get coordinator and group for context. Follow-me always runs for this room when active
        # (with rules for living_room, kristines_room, rooftop); we don't require this speaker to be in the coordinator's group.
        master = self._get_current_coordinator()
        if master:
            members = self.get_state(master, attribute="sonos_group") or []
            if not members:
                members = self.get_state(master, attribute="group_members") or []
        else:
            master = None
            members = []
        
        self.log(f"Scenario: debug_group_members -> master:{master} members:{members}", level="INFO")
        
        # If this room's speaker isn't in the coordinator's group, use its own group or treat as solo and still apply.
        if speaker_entity not in members:
            speaker_members = (self.get_state(speaker_entity, attribute="sonos_group") or
                              self.get_state(speaker_entity, attribute="group_members") or [])
            if len(speaker_members) > 1 and speaker_entity in speaker_members:
                master = speaker_members[0]
                members = speaker_members
                self.log(f"Scenario: using_speaker_group -> {speaker_entity} using its group master:{master} members:{members}", level="INFO")
            else:
                # Solo speaker or no group: still apply follow-me to this room (always run for bathroom, bedroom, etc.)
                master = speaker_entity
                members = [speaker_entity]
                self.log(f"Scenario: follow_me_room -> applying to {speaker_entity} (room: {room}) solo/own scope", level="INFO")
        
        # Determine if room has presence, preferring the event's 'new' value for standard rooms
        # to avoid races where get_state() still returns the previous value.
        present = None
        # Use _is_present for special cases with derived logic
        if (room == "rooftop" or speaker_entity.endswith("rooftop") or
            (room == "kitchen" and self.special.get("kitchen_or_hallway"))):
            present = self._is_present(room, speaker_entity)
            self.log(f"Scenario: presence_calculation -> {room} presence: {present}", level="INFO")
        else:
            # Use the event's new value when available and sane
            present_values = ["on", "home", "detected", "present", "occupied", "true", "yes"]
            if isinstance(new, str):
                nl = new.lower()
                if nl in ["unavailable", "unknown"]:
                    present = None  # indeterminate, ignore
                else:
                    present = nl in present_values
                self.log(f"Scenario: presence_from_event -> {room} new:{new} present:{present}", level="INFO")
            else:
                # Fallback to sensor read if event value is unusable
                present = self._is_present(room, speaker_entity)
                self.log(f"Scenario: presence_calculation -> {room} presence: {present}", level="INFO")

        # If presence is indeterminate, do not change mute state
        if present is None:
            self.log(f"Scenario: presence_indeterminate -> {room} event:{new} skipping mute change", level="INFO")
            return
        
        # Set mute state based on presence: desired_mute = not present (canonical rule)
        desired_mute = not present
        self._apply_follow_me_mute(speaker_entity, desired_mute, room=room, master=master)

    def _check_settled(self, kwargs):
        self._pending_handle = None
        now = self.datetime().timestamp()
        settled_changes = {}
        
        # Check which changes have settled
        for room, change in list(self._pending_changes.items()):
            if now - change['timestamp'] >= self.settle:
                settled_changes[room] = change
                del self._pending_changes[room]
        
        # If no pending changes, this is a follow_me toggle sync
        if not settled_changes:
            self.log("Scenario: follow_me_sync -> checking all rooms", level="INFO")
            
            # Get current coordinator
            master = self._get_current_coordinator()
            if not master:
                self.log("Scenario: follow_me_sync -> no_active_coordinator", level="WARNING")
                return
                
            # Get current group members
            members = (self.get_state(master, attribute="sonos_group") or
                      self.get_state(master, attribute="group_members") or [])
            
            self.log(f"Scenario: follow_me_sync -> found {len(members)} group members with master {master}", level="INFO")

            # Check each speaker's room for presence and apply canonical rule: desired_mute = not present
            for member in members:
                member_room = self.speaker_map.get(member)
                if not member_room:
                    self.log(f"Scenario: follow_me_sync -> could not find room for {member}", level="WARNING")
                    continue
                present = self._is_present(member_room, member)
                if present is None:
                    self.log(f"Scenario: follow_me_sync -> {member_room} presence indeterminate, skipping", level="INFO")
                    continue
                desired_mute = not present
                self._apply_follow_me_mute(member, desired_mute, room=member_room, master=master)
            return
        
        # Process settled changes: same canonical rule (desired_mute = not present) per room
        master = self._get_current_coordinator()
        for room, change in settled_changes.items():
            sp = change['speaker']
            present = self._is_present(room, sp)
            if present is None:
                self.log(f"Scenario: settled -> {room} presence indeterminate, skipping", level="DEBUG")
                continue
            desired_mute = not present
            self._apply_follow_me_mute(sp, desired_mute, room=room, master=master)

    def _get_current_coordinator(self, use_cache=True):
        """Get current coordinator with caching optimization.
        
        Caches result for 1 second to avoid redundant state queries when called
        multiple times quickly. Cache is invalidated on group changes.
        
        Args:
            use_cache: If False, bypass cache and force fresh lookup
        """
        # Check cache if enabled and valid
        if use_cache and self._cached_coordinator is not None and self._cached_coordinator_time is not None:
            elapsed = (self.datetime() - self._cached_coordinator_time).total_seconds()
            if elapsed < self._coordinator_cache_ttl:
                return self._cached_coordinator
        
        # Cache miss or expired - perform lookup.
        # Coordinator = when solo it's that speaker; when grouped it's the first in the group list (same as frontend).
        # In a group, all members share the same state (playing/paused), so we only need to check one.
        # master_select (dropdown) is used by GroupManager for "largest group" / button grouping; we derive coordinator from playing state only.

        # 1) Any solo speaker playing? (when solo, that speaker is the coordinator)
        for sp in self.args["all_speakers"]:
            if self.get_state(sp) == "playing":
                self.log(f"Scenario: detect_playing_master -> {sp}", level="DEBUG")
                self._cached_coordinator = sp
                self._cached_coordinator_time = self.datetime()
                return sp

        # 2) Any group playing? Coordinator = first in list; all members have same state.
        for sp in self.args["all_speakers"]:
            members = (self.get_state(sp, "sonos_group") or
                      self.get_state(sp, "group_members") or [])
            if len(members) > 1:
                coord = members[0]
                if self.get_state(coord) == "playing":
                    self.log(f"Scenario: detect_group_master -> {coord}", level="DEBUG")
                    self._cached_coordinator = coord
                    self._cached_coordinator_time = self.datetime()
                    return coord
                break  # One group only; if it's paused, no coordinator

        self.log("Scenario: no_active_coordinator", level="WARNING")
        self._cached_coordinator = None
        self._cached_coordinator_time = self.datetime()
        return None
    
    def _invalidate_coordinator_cache(self, entity=None, attribute=None, old=None, new=None, kwargs=None):
        """Invalidate coordinator cache when group/state changes occur."""
        self._cached_coordinator = None
        self._cached_coordinator_time = None

    def _is_speaker_playing(self, entity_id):
        """Check if a speaker is currently playing.
        Returns True if state is 'playing', False otherwise.
        Only mute speakers that are actually playing to avoid muting idle/paused/stopped speakers.
        """
        state = self.get_state(entity_id)
        is_playing = state == "playing"
        if not is_playing:
            self.log(f"Scenario: speaker_not_playing -> {entity_id} state is '{state}', skipping mute operation", level="DEBUG")
        return is_playing

    def _parse_muted(self, val):
        """Return True if value represents muted (boolean or string true/on/yes/1)."""
        if val is None:
            return False
        return str(val).lower() in ("true", "on", "yes", "1")

    def _is_muted(self, entity_id):
        """Return True if speaker is muted, False otherwise. Uses _parse_muted for consistent handling."""
        return self._parse_muted(self.get_state(entity_id, attribute="is_volume_muted"))

    def _apply_follow_me_mute(self, speaker_entity, desired_mute, room=None, master=None):
        """Canonical apply: set speaker mute to desired_mute if allowed.
        Skips if room excluded, speaker not playing, or reset in progress. Uses _is_muted for current state.
        """
        if room is not None and self._is_room_excluded_from_follow_me(room):
            return
        if not self._is_speaker_playing(speaker_entity):
            self.log(f"Scenario: apply_skip_not_playing -> {speaker_entity}", level="DEBUG")
            return
        if self._reset_in_progress:
            self.log(f"Scenario: apply_skip_during_reset -> {speaker_entity}", level="DEBUG")
            return
        current = self._is_muted(speaker_entity)
        if current == desired_mute:
            return
        action = "muting" if desired_mute else "unmuting"
        self.log(f"Scenario: apply_mute -> {action} {speaker_entity}", level="INFO")
        self.call_service("media_player/volume_mute", entity_id=speaker_entity, is_volume_muted=desired_mute)
        if not desired_mute:
            m = master or self._get_current_coordinator()
            if speaker_entity == m:
                self._restore_volume_if_airplay(speaker_entity)
                self._restore_volume_if_airplay(speaker_entity, delay_seconds=1.2)

    # Rooms that do NOT get follow_me mute/unmute when they are the solo playing speaker (e.g. TV in living room).
    _SOLO_EXCLUDED_ROOMS = ("living_room", "kristines_room", "rooftop")

    def _should_follow_me_be_active(self):
        """Determine if follow_me should be active based on automatic detection.
        
        Follow_me is active when:
        1. Speakers are grouped (2+ speakers), OR
        2. Any speaker is playing AND the currently playing speaker is NOT in the solo-excluded list.
        
        When solo, we mute/unmute based on presence for bathroom, guest_bathroom, bedroom, etc.
        We do NOT apply follow_me when the solo playing speaker is living_room, kristines_room, or rooftop.
        
        Returns:
            bool: True if follow_me should be active, False otherwise
        """
        # Check if any speakers are grouped (2+ members)
        master = self._get_current_coordinator()
        if master:
            members = (self.get_state(master, attribute="sonos_group") or
                      self.get_state(master, attribute="group_members") or [])
            if len(members) > 1:
                self.log(f"Scenario: follow_me_active -> grouped speakers detected ({len(members)} members)", level="DEBUG")
                return True
        
        # No coordinator or solo: follow_me only if the playing speaker is not excluded when solo
        if not master:
            self.log(f"Scenario: follow_me_inactive -> no coordinator", level="DEBUG")
            return False
        
        master_room = self.speaker_map.get(master)
        if master_room in self._SOLO_EXCLUDED_ROOMS:
            self.log(f"Scenario: follow_me_inactive -> solo playing speaker is {master_room} (excluded when solo)", level="DEBUG")
            return False
        
        # Solo speaker is bathroom, guest_bathroom, bedroom, etc. - apply follow_me
        self.log(f"Scenario: follow_me_active -> solo playing speaker is {master_room}", level="DEBUG")
        return True

    def _is_room_excluded_from_follow_me(self, room):
        """Check if a room should be excluded from follow_me logic.
        
        Bedroom is NOT excluded - it works the same as bathroom.
        
        Excluded when NOT grouped:
        - kristines_room
        - rooftop
        - living_room
        
        Args:
            room: Room name to check
            
        Returns:
            bool: True if room should be excluded, False otherwise
        """
        # Check if speakers are grouped
        master = self._get_current_coordinator()
        is_grouped = False
        if master:
            members = (self.get_state(master, attribute="sonos_group") or
                      self.get_state(master, attribute="group_members") or [])
            is_grouped = len(members) > 1
        
        # If grouped, no rooms are excluded (bedroom works like bathroom)
        if is_grouped:
            return False
        
        # When NOT grouped, exclude same rooms as solo-excluded (living_room, kristines_room, rooftop)
        if room in self._SOLO_EXCLUDED_ROOMS:
            return True
        
        return False

    def friendly_to_entity(self, friendly_name):
        """Convert a friendly name to its entity_id using YAML config."""
        if friendly_name == "None selected" or not friendly_name:
            return None # Keep consistent with original return type for this method

        # Use the inverse map created from YAML config
        entity_id = self._friendly_to_entity_map.get(friendly_name)
        
        if entity_id:
            return entity_id
        else:
            # Fallback for safety, consistent with GroupManager logic
            self.log(f"Warning: Friendly name '{friendly_name}' not found in _friendly_to_entity_map (derived from YAML in FollowMe). " +
                     "This might indicate an issue with the input_select options or YAML configuration. " +
                     "Returning None.", level="WARNING")
            # Original method had a hardcoded map as ultimate fallback, which we are removing.
            # If strict adherence to YAML is desired, this is correct.
            # Consider if a fallback to HA get_state by friendly_name is needed here as in GroupManager if entity_id is critical.
            return None

    def _on_rooftop_charging_change(self, entity, attribute, old, new, kwargs):
        """Handle changes to the rooftop charging state."""
        self.log(f"Scenario: rooftop_charging_change -> old:{old} new:{new}", level="INFO")
        
        # Only continue if follow_me is on
        if not self._should_follow_me_be_active():
            return
            
        # Get current coordinator and members
        master = self._get_current_coordinator()
        if not master:
            return
            
        members = (self.get_state(master, attribute="sonos_group") or
                  self.get_state(master, attribute="group_members") or [])
                  
        # Check if rooftop is in the group
        rooftop = "media_player.rooftop"
        if rooftop not in members:
            return
            
        # Check presence
        present = self._is_present("rooftop", rooftop)
        self.log(f"Scenario: rooftop_presence_update -> present:{present}", level="INFO")
        if present is None:
            self.log("Scenario: rooftop_presence_update -> indeterminate presence, skipping mute change", level="INFO")
            return
        
        # Set mute state based on presence
        desired_mute = not present
        
        # CRITICAL: Only mute/unmute if speaker is actually playing (saves resources)
        # State change listener will handle unmuting when speaker starts playing
        if not self._is_speaker_playing(rooftop):
            self.log(f"Scenario: rooftop_update_skip_mute_unmute -> {rooftop} not playing, skipping mute/unmute operation", level="INFO")
            return
        
        self._apply_follow_me_mute(rooftop, desired_mute, room="rooftop")

    def _on_sonos_group_update(self, event_name, data, kwargs):
        """Handle direct group update events from GroupManager"""
        if not self._should_follow_me_be_active():
            self.log("Scenario: group_update_received -> follow_me is OFF, ignoring", level="DEBUG")
            return
            
        self.log(f"Scenario: group_update_received -> syncing all speakers", level="INFO")
        
        # The GroupManager sent us the members and master - use them directly
        master = data.get("master")
        members = data.get("group_members", [])
        
        if not master or not members:
            self.log("Scenario: group_update_incomplete -> missing master or members", level="WARNING")
            return
            
        self.log(f"Scenario: group_update_processing -> found {len(members)} members with master {master}", level="INFO")
        
        # Check each speaker's room for presence
        for member in members:
            # 1) find which room this speaker lives in
            member_room = self.speaker_map.get(member)
            if not member_room:
                self.log(f"Scenario: group_update -> could not find room for {member}", level="WARNING")
                continue

            # Check if this room is excluded from follow_me
            if self._is_room_excluded_from_follow_me(member_room):
                self.log(f"Scenario: group_update -> {member_room} is excluded from follow_me, skipping", level="DEBUG")
                continue

            # 2) check presence for this specific room
            present = self._is_present(member_room, member)
            self.log(f"Scenario: group_update -> {member_room} presence: {present}", level="INFO")
            if present is None:
                self.log(f"Scenario: group_update -> {member_room} presence indeterminate, skipping {member}", level="INFO")
                continue

            # 3) set mute state based on presence (canonical rule)
            desired_mute = not present
            self._apply_follow_me_mute(member, desired_mute, room=member_room, master=master)

    def _on_living_room_presence_change(self, entity, attribute, old, new, kwargs):
        """Special handler to update rooftop speaker when living room presence changes."""
        # Only continue if follow_me is on
        follow_me_enabled = self._should_follow_me_be_active()
        if not follow_me_enabled:
            return
            
        # Check if rooftop is docked/charging
        rooftop_charging = self.get_state(self.room_map["rooftop_charging"])
        is_charging = rooftop_charging in ["on", "home"]
        
        if not is_charging:
            # If rooftop is not docked, don't change its state based on living room
            return
            
        # Log the dependency
        self.log(f"Scenario: living_room_presence_update -> living room changed to {new}, rooftop follows when docked", level="INFO")
        
        # Get current coordinator and members
        master = self._get_current_coordinator()
        if not master:
            return
            
        # Try both group attributes
        sonos_group = self.get_state(master, attribute="sonos_group") or []
        group_members = self.get_state(master, attribute="group_members") or []
        
        # Use whichever group list is available
        members = sonos_group if sonos_group else group_members
        
        # Check if rooftop is in the group
        rooftop = "media_player.rooftop"
        if rooftop not in members:
            return
            
        # Set mute state based on living room presence
        if isinstance(new, str) and new.lower() in ["unknown", "unavailable"]:
            # Ignore indeterminate updates
            return
        present = isinstance(new, str) and new.lower() in ["on", "home"]
        desired_mute = not present
        
        # CRITICAL: Only mute/unmute if speaker is actually playing (saves resources)
        self._apply_follow_me_mute(rooftop, desired_mute, room="rooftop")

    def _on_reset_started(self, event_name, data, kwargs):
        """Handle reset started event - pause follow_me mute/unmute operations"""
        self.log("Scenario: reset_started -> pausing follow_me mute/unmute operations", level="INFO")
        if self._reset_resume_handle is not None:
            self._safe_cancel_timer(self._reset_resume_handle)
            self._reset_resume_handle = None
        self._fm_reset_generation += 1
        self._reset_in_progress = True

    def _on_reset_completed(self, event_name, data, kwargs):
        """Hardware reset done - delay follow_me sync until Sonos/GM have settled."""
        self.log(
            f"Scenario: reset_completed -> scheduling follow_me resume in {self.reset_resume_delay}s",
            level="INFO",
        )
        resume_gen = self._fm_reset_generation
        if self._reset_resume_handle is not None:
            self._safe_cancel_timer(self._reset_resume_handle)
        self._reset_resume_handle = self.run_in(
            self._delayed_reset_sync,
            self.reset_resume_delay,
            session_gen=resume_gen,
        )

    def _delayed_reset_sync(self, kwargs):
        """Clear reset gate and sync mutes after shared delay with GroupManager."""
        if not isinstance(kwargs, dict):
            kwargs = {}
        session_gen = kwargs.get("session_gen")
        if session_gen is not None and session_gen != self._fm_reset_generation:
            self.log(
                f"Scenario: reset_sync_stale -> skip (session_gen={session_gen}, current={self._fm_reset_generation})",
                level="DEBUG",
            )
            self._reset_resume_handle = None
            return
        self._reset_resume_handle = None
        self.log("Scenario: reset_completed_delayed -> follow_me resuming", level="INFO")
        self._reset_in_progress = False
        self._trigger_follow_me_sync("reset_completed_delayed")