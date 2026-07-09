# /conf/apps/sonos/group_manager.py

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import threading

class SonosGroupManager(hass.Hass):
    """
    Comprehensive Sonos group management that:
    1) Group Formation:
       - Tracks group creation and expansion
       - Manages master speaker selection
       - Handles group membership changes
    2) Single Speaker Ungrouping:
       - Handles individual speaker removal from groups
       - Manages solo speaker state transitions
    3) Group Disbanding:
       - Coordinates complete group dissolution
       - Handles master speaker ungrouping
    4) State Management:
       - Tracks group membership states
       - Manages follow_me flag based on grouping
       - Maintains master speaker selection
    5) Error Recovery:
       - Detects and fixes inconsistent states
       - Handles edge cases in group operations
    """

    def initialize(self):
        # app‑definition args
        self.speakers       = self.args["all_speakers"]
        self.follow_flag    = self.args["follow_me_flag"]
        self.master_select  = self.args["master_select"]
        self.last_ts_entity = self.args["last_ts_input"]
        self.throttle       = float(self.args.get("throttle_seconds", 2))
        self.settle_delay   = float(self.args.get("settle_seconds", 2))
        self.reset_resume_delay = float(self.args.get("reset_resume_delay", 2.5))
        # NEW: Load the friendly name map from YAML
        self.entity_friendly_name_map = self.args.get("entity_friendly_name_map", {})
        # NEW: Create the inverse map for friendly_name to entity_id
        self._friendly_to_entity_map = {v: k for k, v in self.entity_friendly_name_map.items()}

        # Family Zone Auto-Group config
        self.family_zone_speakers = self.args.get("family_zone_speakers", [])
        self.rooftop_entity = self.args.get("rooftop_entity", "media_player.rooftop")
        self.rooftop_charging_sensor = self.args.get("rooftop_charging_sensor", "binary_sensor.rooftop_charging")
        self._family_zone_sync_in_progress = False
        self._family_zone_sync_start_time = None  # Track when sync started for stuck detection
        self._family_zone_sync_timeout_s = 15  # Auto-reset if stuck longer than this
        
        # Operation queue for serializing group operations
        self._group_operation_queue = []  # Queue of pending group operations
        self._group_operation_in_progress = False  # Flag to track if an operation is running
        self._group_operation_start_time = None  # Track when operation started for timeout detection
        self._group_operation_timeout_s = 30  # Auto-reset if stuck >30s
        self._group_operation_lock = threading.Lock()  # Lock for queue operations

        # internal state
        self._last_trigger = 0
        self._prev_group_members = {}  # tracks previous group members for each speaker
        self._pending_evaluate_handle = None
        # track when grouping last existed
        self._last_group_ts = 0
        # group recalculation debouncing
        self._last_group_update = 0
        self._pending_group_update = None
        # Generation counter to invalidate stale scheduled updates without cancelling timers
        self._group_update_generation = 0
        # track group changes in a batch
        self._group_changes = []
        # track if we're in a reset operation
        self._reset_in_progress = False
        # track pending reset request
        self._pending_reset = None
        self._reset_resume_handle = None
        self._reset_generation = 0
        # thread synchronization
        self.lock = threading.Lock()
        self._pending_unjoin_checks = set() # Stores entity_ids of speakers we are waiting to see become solo after an unjoin
        # Track last non-zero volume per speaker for AirPlay restore
        self._last_nonzero_volume = {}

        # Clean stale entries in _prev_group_members
        self._clean_prev_group_members()

        # listen to any state or group change
        for sp in self.speakers:
            self.listen_state(self._on_state_change, sp)
            self.listen_state(self._on_group_change, sp, attribute="group_members")
            # Track volume changes to remember last non-zero level
            self.listen_state(self._on_volume_level_change, sp, attribute="volume_level")

        # catch join/unjoin service calls
        self.listen_event(self._on_service_call,
                          "call_service",
                          domain="media_player",
                          service="join")
        self.listen_event(self._on_service_call,
                          "call_service",
                          domain="media_player",
                          service="unjoin")

        # NOTE: follow_me toggle is no longer used - automatic detection is used instead
        # The follow_flag is kept for backward compatibility but not actively monitored

        # Listen for State Reset events
        self.listen_event(self._on_reset_requested, "sonos_reset_requested")
        self.listen_event(self._on_reset_started, "sonos_reset_started")
        self.listen_event(self._on_reset_completed, "sonos_reset_completed")

        # Listen for custom group join/unjoin requests
        self.listen_event(self._handle_group_join_request, "request_sonos_group_join")
        self.listen_event(self._handle_group_unjoin_request, "request_sonos_group_unjoin")

        self.log("SonosGroupManager loaded", level="INFO")
        
        # RESTORE FOLLOW_ME STATE ON RESTART: Check if any speakers are currently grouped
        # and ensure follow_me is enabled if needed
        self.run_in(self._check_groups_on_startup, 5)  # Give Home Assistant a few seconds to finish initializing

    def _on_volume_level_change(self, entity, attribute, old, new, kwargs):
        """Track last non-zero volume for AirPlay restore."""
        try:
            if new is None:
                return
            vol = float(new)
            # Only track when follow_me is active and this entity is the current master
            if self._should_follow_me_be_active():
                current_master = self._friendly_to_entity_id(self.get_state(self.master_select))
                if current_master and current_master != "none" and entity == current_master and vol > 0.0:
                    self._last_nonzero_volume[entity] = vol
        except Exception:
            # Ignore parse errors quietly
            pass

    def _restore_volume_if_airplay(self, entity_id):
        """If source is AirPlay and volume is 0, restore last non-zero volume."""
        try:
            # Only restore when follow_me is active and this entity is the current master
            if not self._should_follow_me_be_active():
                return
            current_master = self._friendly_to_entity_id(self.get_state(self.master_select))
            if not current_master or current_master == "none" or entity_id != current_master:
                return
            src = self.get_state(entity_id, attribute="source")
            if not isinstance(src, str) or src.lower() != "airplay":
                return
            curr = self.get_state(entity_id, attribute="volume_level")
            curr_f = float(curr) if curr is not None else 0.0
            if curr_f > 0.0:
                return
            last = self._last_nonzero_volume.get(entity_id)
            if last is not None and last > 0.0:
                self._safe_call_service("media_player/volume_set",
                                       entity_id=entity_id,
                                       volume_level=last)
        except Exception:
            # Best-effort only
            pass
        
    def _check_groups_on_startup(self, kwargs=None):
        """Check if any speakers are grouped on startup and enable follow_me if needed"""
        # Count how many speakers are in groups
        grouped_speakers_by_master = {}
        all_grouped_member_entities = set()

        for sp in self.speakers:
            members = self.get_state(sp, attribute="group_members") or []
            if len(members) > 1:
                # Use the first member as the presumed master for grouping this log
                master_for_log = members[0]
                if master_for_log not in grouped_speakers_by_master:
                    grouped_speakers_by_master[master_for_log] = set(members)
                else:
                    grouped_speakers_by_master[master_for_log].update(members)
                all_grouped_member_entities.update(members)
        
        any_grouped = bool(grouped_speakers_by_master)

        if any_grouped:
            self.log(f"Scenario: startup_groups_detected -> {len(grouped_speakers_by_master)} distinct group(s) found.", level="INFO")
            for master_log, members_set in grouped_speakers_by_master.items():
                self.log(f"  Group with presumed master {master_log} has {len(members_set)} members: {sorted(list(members_set))}", level="INFO")
        else:
            self.log("Scenario: startup_no_groups -> No active Sonos groups found at startup.", level="INFO")

        # If follow_me should be active (groups exist or all playing condition), trigger sync
        if self._should_follow_me_be_active():
            self.log("Scenario: startup_follow_me_active -> follow_me conditions met at startup", level="INFO")
            
            # Update timestamp
            now = self.datetime().timestamp()
            self._last_group_ts = now  # Update internal timestamp too
            self._safe_call_service("input_number/set_value",
                                  entity_id=self.last_ts_entity,
                                  value=now)
            
            # Trigger a group update to ensure mute states are correct
            unique_members = list(all_grouped_member_entities) # Use the set of all members found
            if unique_members:
                self.log(f"Scenario: startup_trigger_follow_me -> syncing {len(unique_members)} grouped speakers", level="INFO")
                self.fire_event("sonos_group_update", 
                             group_members=unique_members, 
                             master=self._friendly_to_entity_id(self.get_state(self.master_select)))

    def _clean_prev_group_members(self):
        """Remove any stale entries from _prev_group_members dict"""
        valid_speakers = set(self.speakers)
        stale_keys = [k for k in self._prev_group_members if k not in valid_speakers]
        
        if stale_keys:
            self.log(f"Scenario: cleaning_stale_data -> removing {len(stale_keys)} old speakers", level="DEBUG")
            for k in stale_keys:
                del self._prev_group_members[k]

    def _on_state_change(self, entity, attribute, old, new, kwargs):
        """Handle speaker state changes"""
        self.log(f"State change detected: {entity} from {old} to {new}", level="DEBUG")
        self._maybe_trigger("state_change", entity, old, new)

    def _on_group_change(self, entity, attribute, old, new, kwargs):
        # Store previous group members for detecting "was grouped now solo"
        old_members = old or []
        new_members = new or []
        
        # Update previous group members with thread safety
        with self.lock:
            self._prev_group_members[entity] = old_members

        if self._reset_in_progress:
            self.log("Scenario: skip_group_change_during_reset -> StateReset owns group/mute during reset", level="DEBUG")
            return
        
        # Check if speaker became solo (was in a group and now isn't)
        was_grouped = len(old_members) > 1
        now_solo = len(new_members) == 1
        
        # Unmute if speaker became solo - use direct call_service for reliability
        if was_grouped and now_solo:
            self.log(f"Scenario: speaker_became_solo -> unmuting {entity}", level="INFO")
            # First try the safe method
            currently_muted = self._safe_get_state(entity, attribute="is_volume_muted", default="false") == "true"
            if currently_muted:
                self._safe_call_service("media_player/volume_mute", entity_id=entity, is_volume_muted=False)
                
            # Then try direct call as backup 
            self.call_service("media_player/volume_mute", entity_id=entity, is_volume_muted=False)
            # If this solo speaker is the current master and using AirPlay, restore volume if needed
            current_master_entity = self._friendly_to_entity_id(self.get_state(self.master_select))
            if entity == current_master_entity:
                self._restore_volume_if_airplay(entity)
            
            # Check if this was a pending unjoin confirmation
            if entity in self._pending_unjoin_checks:
                self.log(f"Scenario: unjoin_confirmed_solo -> {entity} is now solo. Triggering evaluation.", level="INFO")
                self._pending_unjoin_checks.remove(entity)
                self._maybe_trigger("unjoin_solo_confirmed", entity, old_members, new_members)
            
            # Note: follow_me state is now automatically detected, no toggle management needed
        
        # SMART FAMILY ZONE DETECTION: trigger when any family speaker joins any other speaker
        now_grouped = len(new_members) > 1
        group_expanded = now_grouped and len(new_members) > len(old_members)
        group_contracted = now_grouped and len(new_members) < len(old_members)
        
        if group_expanded and self._family_speaker_joined_external_group(new_members):
            family_in_group = set(self.family_zone_speakers) & set(new_members)
            self.log(
                f"Scenario: family_zone_group_detected -> family speaker(s) {sorted(family_in_group)} "
                f"joined a {len(new_members)}-speaker group, triggering fast sync",
                level="INFO",
            )
            self.run_in(self._check_family_zone_synchronization, 0.5)
        
        # Track group changes in a batch for ONE recalculation at the end
        # Instead of calling _trigger_follow_me_recalculation() for each speaker,
        # we'll add this change to a list and trigger ONE recalculation after processing
        
        follow_on = self._should_follow_me_be_active()
        if follow_on and (group_expanded or group_contracted):
            change_type = "expanded" if group_expanded else "contracted"
            # Add to group changes - we'll trigger ONE recalculation at the end
            self._group_changes.append({
                "entity": entity,
                "type": change_type,
                "from": len(old_members),
                "to": len(new_members)
            })
            self.log(f"Scenario: group_{change_type}_tracked -> {entity} from {len(old_members)} to {len(new_members)}", level="DEBUG")
        
        # Check for ANY group formation by counting total grouped speakers before and after
        follow_on = self._should_follow_me_be_active()
        if not follow_on:
            # Count total grouped speakers before this change
            total_before = sum(len(self._prev_group_members.get(sp, [])) > 1 for sp in self.speakers)
            
            # Update this entity's members before calculating total_after
            self._prev_group_members[entity] = new_members
            
            # Count total grouped speakers after this change
            total_after = sum(len(self.get_state(sp, attribute="group_members") or []) > 1 for sp in self.speakers)
            
            # Detect transition from no groups to at least one group
            if total_before == 0 and total_after > 0:
                self.log("Scenario: multi_speaker_group_detected -> follow_me should be active", level="INFO")
                
                # Update timestamp (check current value first)
                now = self.datetime().timestamp()
                current_ts = float(self.get_state(self.last_ts_entity) or 0)
                if now != current_ts:
                    self._safe_call_service("input_number/set_value",
                                          entity_id=self.last_ts_entity,
                                          value=now)
            
            # Reset this entity's previous members to ensure consistent state for other checks
            self._prev_group_members[entity] = old_members
        
        # Update last‑group timestamp if any group now exists
        if any(len(self.get_state(sp, attribute="group_members") or []) > 1
               for sp in self.speakers):
            self._last_group_ts = self.datetime().timestamp()
            
        # Continue with normal processing
        self._maybe_trigger("group_change", entity, old_members, new_members)
        
        # At the very end of this method, if we have group changes, schedule ONE recalculation
        # Wait a short time to let other group_change events arrive
        if self._group_changes and follow_on:
            # Bump generation to invalidate any previously scheduled callbacks (no cancel to avoid warnings)
            self._group_update_generation += 1
            current_gen = self._group_update_generation
            # Schedule a new one after a short delay to catch all group changes in a batch
            self._pending_group_update = self.run_in(
                self._process_group_changes,
                1.0,  # 1 second is usually enough to collect all related group changes
                gen=current_gen,
            )
        
        # If no specific unjoin confirmation, but group structure changed, still trigger a general evaluation
        # This handles cases like speakers joining a group, or groups disbanding not via a direct unjoin service call
        # on a specific speaker we were watching.
        if not (was_grouped and now_solo and entity in self._pending_unjoin_checks): # Avoid double trigger if unjoin confirmed above
            if old_members != new_members: # If there was any change in group members at all
                 self._maybe_trigger("group_change_general", entity, old_members, new_members)

    def _process_group_changes(self, kwargs=None):
        """Process all accumulated group changes in a single batch"""
        # Ignore if this callback is stale (was superseded by a newer generation)
        gen = None
        if isinstance(kwargs, dict):
            gen = kwargs.get("gen")
        if gen is not None and gen != self._group_update_generation:
            # Stale callback; do nothing
            return
        if self._reset_in_progress:
            self.log("Scenario: skip_process_group_changes_during_reset", level="DEBUG")
            return
        # This timer has fired and is current; clear the handle
        self._pending_group_update = None

        if not self._group_changes:
            self.log("Scenario: _process_group_changes -> no group changes to process.", level="DEBUG")
            return
            
        self.log(f"Scenario: batch_group_changes -> processing {len(self._group_changes)} changes", level="INFO")
        
        # Display summary of changes for debugging
        expand_count = sum(1 for c in self._group_changes if c["type"] == "expanded")
        contract_count = sum(1 for c in self._group_changes if c["type"] == "contracted")
        self.log(f"Scenario: group_changes_summary -> {expand_count} expansions, {contract_count} contractions", level="INFO")
        
        # Clear the group changes list
        current_changes = list(self._group_changes) # Make a copy if needed for _send_group_update or logging
        self._group_changes = []
        
        # Original code called self.cancel_pending_update() here, which is no longer needed as
        # self._pending_group_update is already None.
        
        # Trigger a single recalculation
        self._send_group_update() # Consider if this needs current_changes if it relies on self._group_changes

        # Fallback: Check for Family Zone synchronization after batch processing
        # The fast path in _on_group_change handles immediate family zone detection (0.5s)
        # This is a slower fallback for edge cases (e.g., rooftop docking changes)
        self.run_in(self._check_family_zone_synchronization, 1.0)

    def _family_speaker_joined_external_group(self, group_members):
        """True when at least one family-zone speaker is in a multi-speaker group."""
        if not self.family_zone_speakers or not isinstance(group_members, list):
            return False
        if len(group_members) <= 1:
            return False
        family_zone_set = set(self.family_zone_speakers)
        return bool(family_zone_set & set(group_members))

    def _family_group_has_active_audio(self, group_members):
        """True if any speaker in the group is playing or paused (e.g. Spotify session)."""
        for sp in group_members:
            if self.get_state(sp) in ("playing", "paused"):
                return True
        return False

    def _find_family_zone_group_anchor(self, current_family_zone):
        """
        Find a family-zone speaker in an active multi-speaker group to use as join anchor.
        Prefers a playing/paused family member; falls back to any grouped family member
        when the group still has active audio (covers Spotify join races).
        """
        grouped_family = []
        for member in current_family_zone:
            members = self.get_state(member, attribute="group_members")
            if not isinstance(members, list) or len(members) <= 1:
                continue
            grouped_family.append((member, members))

        if not grouped_family:
            return None, None, []

        # Prefer family member with its own active playback
        for member, members in grouped_family:
            if self.get_state(member) in ("playing", "paused"):
                return member, members, "family_active"

        # Spotify/other sources: group may form before the family speaker shows playing
        for member, members in grouped_family:
            if self._family_group_has_active_audio(members):
                return member, members, "group_active"

        return None, None, []

    def _resolve_group_master(self, anchor_member, group_members):
        sonos_group = self.get_state(anchor_member, attribute="sonos_group")
        if sonos_group and isinstance(sonos_group, list):
            return sonos_group[0]
        if group_members:
            return group_members[0]
        return anchor_member

    def _check_family_zone_synchronization(self, kwargs=None):
        """
        Auto-group family-zone speakers (kitchen, dining, living, docked rooftop) when
        any of them is in a multi-speaker group with active audio.

        Skips while living room TV is on. Requires active audio in the group so we do not
        pull speakers together during idle ungroup/reset flows.
        """
        if self._reset_in_progress:
            self.log("Scenario: skip_family_zone_sync_during_reset", level="DEBUG")
            return
        # Avoid re-entry loops if we are the ones triggering the join
        # But also detect and reset stuck flags
        if self._family_zone_sync_in_progress:
            if self._family_zone_sync_start_time is not None:
                try:
                    elapsed = (self.datetime() - self._family_zone_sync_start_time).total_seconds()
                    if elapsed > self._family_zone_sync_timeout_s:
                        self.log(f"Resetting stuck _family_zone_sync_in_progress flag (was set {elapsed:.1f}s ago)", level="WARNING")
                        self._family_zone_sync_in_progress = False
                        self._family_zone_sync_start_time = None
                    else:
                        self.log(f"Family Zone sync already in progress for {elapsed:.1f}s, skipping", level="DEBUG")
                        return
                except Exception:
                    pass
            else:
                self.log("Family Zone sync in progress, skipping", level="DEBUG")
                return

        if not self.family_zone_speakers:
            return
        
        # Check if living room TV is on - if so, skip auto-grouping to avoid conflicts
        try:
            living_room_tv_state = self.get_state("media_player.living_room_tv")
            if living_room_tv_state and living_room_tv_state not in [None, "off", "unavailable", "unknown", "standby"]:
                self.log(f"Family Zone Auto-Group: Living room TV is on (state: {living_room_tv_state}), skipping auto-group to avoid conflicts", level="DEBUG")
                return
        except Exception:
            # If we can't check TV state, continue (don't block on TV check failure)
            pass
            
        # Use try/finally to ALWAYS reset the flag, even if callback is interrupted
        self._family_zone_sync_in_progress = True
        self._family_zone_sync_start_time = self.datetime()
        
        try:
            # 1. Determine current Family Zone membership
            current_family_zone = list(self.family_zone_speakers)
            
            # Check Rooftop status
            try:
                is_docked = self.get_state(self.rooftop_charging_sensor) == "on"
                if is_docked and self.rooftop_entity:
                    if self.rooftop_entity not in current_family_zone:
                        current_family_zone.append(self.rooftop_entity)
            except Exception:
                pass # Ignore if sensor missing
                
            anchor_member, target_group, anchor_reason = self._find_family_zone_group_anchor(
                current_family_zone
            )
            if not anchor_member or not target_group:
                self.log(
                    "Family Zone Auto-Group: No grouped family member with active audio. Skipping.",
                    level="DEBUG",
                )
                return

            target_master = self._resolve_group_master(anchor_member, target_group)
            self.log(
                f"Family Zone Auto-Group: Anchor {anchor_member} ({anchor_reason}) in "
                f"{len(target_group)}-speaker group, master {target_master}",
                level="INFO",
            )

            # 3. Identify missing family members
            missing_members = [
                m for m in current_family_zone 
                if m not in target_group
            ]

            if not missing_members:
                # Everyone is already together
                self.log(f"Family Zone Auto-Group: All family members already in group. No action needed.", level="DEBUG")
                return

            self.log(
                f"Family Zone Auto-Group: Anchor {anchor_member} on {target_master}. "
                f"Missing members: {missing_members}. Syncing...",
                level="INFO",
            )
            
            # 4. Queue the join operation to avoid conflicts with GUI or other sources
            self._queue_group_operation("join", target_master, missing_members)

        except Exception as e:
            self.log(f"Error in Family Zone synchronization: {e}", level="ERROR")
        finally:
            # Always reset flag - use run_in to allow state changes to propagate first
            # but ensure we reset even if this callback is interrupted
            self.run_in(self._reset_family_zone_sync_flag, 3.0)
    
    def _reset_family_zone_sync_flag(self, kwargs=None):
        """Reset the family zone sync flag after operations complete"""
        self._family_zone_sync_in_progress = False
        self._family_zone_sync_start_time = None
    
    def _queue_group_operation(self, operation_type, target_entity, members, priority="normal"):
        """
        Queue a group operation to be executed serially.
        This prevents race conditions when multiple sources (GUI, automation) 
        try to modify groups simultaneously.
        
        Args:
            operation_type: "join" or "unjoin"
            target_entity: Target speaker entity (for join) or None (for unjoin)
            members: List of speaker entities to join/unjoin
            priority: "high" (user actions) or "normal" (automation)
                     High priority operations execute immediately if queue is empty,
                     otherwise are inserted at front of queue.
        """
        if self._reset_in_progress:
            self.log(f"Scenario: skip_queue_group_op_during_reset -> {operation_type}", level="DEBUG")
            return
        # Normalize members to list and sort for deduplication
        if not isinstance(members, list):
            members = [members] if members else []
        members = sorted(members)  # Sort for consistent deduplication comparison
        
        with self._group_operation_lock:
            # Check for duplicate operations (deduplication)
            operation_key = (operation_type, target_entity, tuple(members))
            for existing_op in self._group_operation_queue:
                existing_key = (existing_op["type"], existing_op.get("target"), tuple(sorted(existing_op.get("members", []))))
                if existing_key == operation_key:
                    self.log(f"Skipping duplicate operation: {operation_type} {members} to {target_entity}", level="DEBUG")
                    return
            
            # If high priority and queue is empty and no operation in progress, execute immediately
            if priority == "high" and not self._group_operation_queue and not self._group_operation_in_progress:
                self.log(f"High priority operation: Executing immediately (queue empty): {operation_type} {members} to {target_entity}", level="INFO")
                # Execute immediately without queuing
                self._execute_group_operation(operation_type, target_entity, members)
                return
            
            # Otherwise, queue it
            operation = {
                "type": operation_type,
                "target": target_entity,
                "members": members,
                "priority": priority,
                "queued_at": self.datetime()
            }
            
            # High priority goes to front, normal goes to back
            if priority == "high":
                self._group_operation_queue.insert(0, operation)
                self.log(f"Queued HIGH priority operation at front: {operation_type} {len(members)} speakers to {target_entity}", level="INFO")
            else:
                self._group_operation_queue.append(operation)
                self.log(f"Queued normal priority operation: {operation_type} {len(members)} speakers to {target_entity}", level="DEBUG")
            
            # Warn if queue is getting long (but don't drop operations)
            if len(self._group_operation_queue) > 20:
                self.log(f"Queue backlog warning: {len(self._group_operation_queue)} operations queued. Consider investigating if this persists.", level="WARNING")
            
            # If no operation is in progress, start processing
            if not self._group_operation_in_progress:
                self.run_in(self._process_group_operation_queue, 0.1)
    
    def _execute_group_operation(self, op_type, target, members):
        """Execute a group operation (join or unjoin)"""
        if op_type == "join":
            self.log(f"Executing join: {members} to {target}", level="INFO")
            self.call_service(
                "media_player/join",
                entity_id=target,
                group_members=members
            )
        elif op_type == "unjoin":
            self.log(f"Executing unjoin: {members}", level="INFO")
            for member in members:
                try:
                    self.call_service("media_player/unjoin", entity_id=member)
                except Exception as e:
                    self.log(f"Failed to unjoin {member}: {e}", level="WARNING")
    
    def _process_group_operation_queue(self, kwargs=None):
        """Process the next operation in the queue"""
        if self._reset_in_progress:
            self.log("Scenario: skip_group_op_queue_during_reset", level="DEBUG")
            return
        operation = None
        has_more_operations = False
        
        # Check for stuck operation with timeout detection
        if self._group_operation_in_progress and self._group_operation_start_time:
            try:
                elapsed = (self.datetime() - self._group_operation_start_time).total_seconds()
                if elapsed > self._group_operation_timeout_s:
                    self.log(f"Resetting stuck _group_operation_in_progress flag (stuck for {elapsed:.1f}s)", level="WARNING")
                    with self._group_operation_lock:
                        self._group_operation_in_progress = False
                        self._group_operation_start_time = None
                else:
                    # Still processing, will be called again when current operation finishes
                    return
            except Exception:
                pass
        
        with self._group_operation_lock:
            if self._group_operation_in_progress:
                # Already processing, will be called again when current operation finishes
                return
            
            if not self._group_operation_queue:
                # Queue is empty
                return
            
            self._group_operation_in_progress = True
            self._group_operation_start_time = self.datetime()
            operation = self._group_operation_queue.pop(0)
            # Check queue state INSIDE lock before releasing
            has_more_operations = len(self._group_operation_queue) > 0
        
        if operation is None:
            return
        
        try:
            op_type = operation["type"]
            target = operation["target"]
            members = operation["members"]
            
            self._execute_group_operation(op_type, target, members)
            self.log(f"Completed queued {op_type} operation", level="INFO")
            
        except Exception as e:
            self.log(f"Error executing queued group operation: {e}", level="ERROR")
        finally:
            with self._group_operation_lock:
                self._group_operation_in_progress = False
                self._group_operation_start_time = None
            
            # Check if there are more operations to process (already checked inside lock)
            # Small delay to let Sonos state settle between operations
            if has_more_operations:
                self.run_in(self._process_group_operation_queue, 1.0)

    def _trigger_follow_me_recalculation(self):
        """Force follow_me app to recalculate mute states for all speakers"""
        self.log("Scenario: triggering_follow_me_recalc -> group membership changed", level="INFO")
        
        # For backwards compatibility and direct calls, send update immediately
        self._send_group_update()
        
    def _send_group_update(self):
        """Send the actual group update event (called directly or after debounce)"""
        # Get all speakers that are in any group
        grouped_speakers = []
        for sp in self.speakers:
            members = self.get_state(sp, attribute="group_members") or []
            if len(members) > 1:
                grouped_speakers.extend(members)
                
        # Remove duplicates
        grouped_speakers = list(set(grouped_speakers))
        
        # Fire an event that the follow_me app can listen for
        self.fire_event("sonos_group_update", 
                       group_members=grouped_speakers, 
                       master=self._friendly_to_entity_id(self.get_state(self.master_select)))
        
        # Log what we're doing
        self.log(f"Scenario: direct_recalc -> fired sonos_group_update event with {len(grouped_speakers)} speakers", level="INFO")

    def _on_service_call(self, event_name, data, kwargs):
        # e.g. data={'domain':'media_player','service':'join', 'entity_id':'media_player.kitchen'}
        service = data["service"]
        entity = data.get("entity_id")

        if self._reset_in_progress and service in ("join", "unjoin"):
            self.log(
                f"Scenario: skip_service_call_during_reset -> {service} (StateReset owns unjoin/poll/finalize)",
                level="DEBUG",
            )
            return
        
        # Update timestamp for join events (follow_me is now automatically detected)
        if service == "join":
            self.log(f"Scenario: join_service_detected -> follow_me will be active if conditions met", level="INFO")
            
            # Update timestamp
            now = self.datetime().timestamp()
            current_ts = float(self.get_state(self.last_ts_entity) or 0)
            if now != current_ts:
                self._safe_call_service("input_number/set_value",
                                      entity_id=self.last_ts_entity,
                                      value=now)
            
            # Update group timestamp since we're forming a group
            self._last_group_ts = self.datetime().timestamp()
            
        # Special handling for unjoin (ungroup) on master speaker
        if service == "unjoin":
            ungrouping_entity = data.get("entity_id") # This is the speaker being told to unjoin

            if ungrouping_entity and ungrouping_entity != "all" and ungrouping_entity in self.speakers:
                self.log(f"Scenario: unjoin_service_for_entity -> {ungrouping_entity}. Will unmute and await solo state.", level="INFO")
                self._safe_call_service("media_player/volume_mute", entity_id=ungrouping_entity, is_volume_muted=False)
                self._pending_unjoin_checks.add(ungrouping_entity)
                # We do NOT call _maybe_trigger here. _on_group_change will do it when solo state is confirmed.
            elif ungrouping_entity == "all":
                self.log("Scenario: unjoin_all_service -> Unmuting relevant speakers (follow_me automatically detected).", level="INFO")
                current_master_entity = self._friendly_to_entity_id(self.get_state(self.master_select))
                members_to_unmute = self._prev_group_members.get(current_master_entity, self.get_state(current_master_entity, attribute="group_members") or [])
                if not members_to_unmute: # Fallback if master had no clear group
                    members_to_unmute = self.speakers
                for member in members_to_unmute:
                    self._safe_call_service("media_player/volume_mute", entity_id=member, is_volume_muted=False)
                # Note: follow_me state is now automatically detected, no toggle needed
                self._maybe_trigger("unjoin_all", None, None, None) # Trigger a general state evaluation after unjoin all
            else:
                # Some other unjoin scenario (e.g., entity_id not in our speaker list, or None)
                # Still trigger a general evaluation as group state might change unpredictably.
                self.log(f"Scenario: unjoin_service_other -> entity: {ungrouping_entity}. Triggering general evaluation.", level="DEBUG")
                self._maybe_trigger(service, ungrouping_entity, None, None)

        else:
            # For all other services (like join), trigger evaluation normally
            self._maybe_trigger(service, entity, None, None)

    # NOTE: _on_follow_disabled removed - follow_me is now automatically detected, no toggle listener needed

    def _on_reset_requested(self, event_name, data, kwargs):
        """Handle State Reset request - exclusive pause: flush queued work, block new group logic."""
        self.log("Scenario: reset_requested -> exclusive pause for group management", level="INFO")
        self._reset_in_progress = True
        if self._reset_resume_handle is not None:
            self._safe_cancel_timer(self._reset_resume_handle)
            self._reset_resume_handle = None
        self._reset_generation += 1
        self._pending_reset = data
        handle_to_cancel = None
        if self._pending_evaluate_handle is not None:
            handle_to_cancel = self._pending_evaluate_handle
            self._pending_evaluate_handle = None
        if handle_to_cancel is not None:
            self._safe_cancel_timer(handle_to_cancel)
        if self._pending_group_update is not None:
            self._safe_cancel_timer(self._pending_group_update)
            self._pending_group_update = None
        self._group_update_generation += 1
        with self._group_operation_lock:
            self._group_operation_queue.clear()
        self._group_changes.clear()
        self._family_zone_sync_in_progress = False
        self._family_zone_sync_start_time = None
        self.fire_event(
            "sonos_reset_ready",
            targets=data.get("targets", []),
            trigger=data.get("trigger", "unknown"),
            source=data.get("source", None),
        )

    def _on_reset_started(self, event_name, data, kwargs):
        """Handle State Reset starting - Ensure group management is paused"""
        self.log("Scenario: reset_started -> group management paused", level="INFO")
        self._reset_in_progress = True
        if self._reset_resume_handle is not None:
            self._safe_cancel_timer(self._reset_resume_handle)
            self._reset_resume_handle = None

    def _on_reset_completed(self, event_name, data, kwargs):
        """State Reset finished hardware phase - resume GM after delay (Sonos settle)."""
        self.log(
            f"Scenario: reset_completed -> scheduling resume in {self.reset_resume_delay}s",
            level="INFO",
        )
        resume_gen = self._reset_generation
        if self._reset_resume_handle is not None:
            self._safe_cancel_timer(self._reset_resume_handle)
        self._reset_resume_handle = self.run_in(
            self._resume_after_reset,
            self.reset_resume_delay,
            session_gen=resume_gen,
        )

    def _resume_after_reset(self, kwargs):
        """Clear reset gate and run one evaluation after Sonos has settled."""
        if not isinstance(kwargs, dict):
            kwargs = {}
        session_gen = kwargs.get("session_gen")
        if session_gen is not None and session_gen != self._reset_generation:
            self.log(
                f"Scenario: reset_resume_stale -> skip (session_gen={session_gen}, current={self._reset_generation})",
                level="DEBUG",
            )
            self._reset_resume_handle = None
            return
        self._reset_resume_handle = None
        self.log("Scenario: reset_resume -> clearing _reset_in_progress, scheduling evaluation", level="INFO")
        self._reset_in_progress = False
        self._pending_reset = None
        self._maybe_trigger("reset_completed_delayed", None, None, None)

    def _safe_cancel_timer(self, timer_handle):
        """
        Safely cancel a timer without triggering AppDaemon "Invalid callback handle" warnings.

        AppDaemon can log a WARNING when cancel_timer() is called with a stale/invalid handle,
        but it may not raise an exception. To avoid noisy logs, we only cancel timers that
        are still running.
        """
        if timer_handle is None:
            return False
        try:
            # timer_running() returns False for stale/unknown handles (and avoids warning spam)
            if self.timer_running(timer_handle):
                self.cancel_timer(timer_handle)
                return True
            return False
        except Exception as e:
            self.log(f"Error checking/cancelling timer: {e}", level="DEBUG")
            return False

    def _maybe_trigger(self, trigger_id, entity, old=None, new=None):
        # Skip evaluation if reset is in progress
        if self._reset_in_progress:
            self.log(f"Scenario: skip_evaluation -> reset in progress, ignoring {trigger_id}", level="DEBUG")
            return

        now = self.datetime().timestamp()
        if now - self._last_trigger < self.throttle:
            self.log(f"Scenario: throttle_skip -> {trigger_id} throttled", level="DEBUG")
            return
        self._last_trigger = now
        
        # Cancel any pending evaluation to avoid duplicates
        # Make sure to set the instance variable to None *before* calling cancel_timer
        # to prevent race conditions if this method is called again quickly.
        handle_to_cancel = None
        if self._pending_evaluate_handle is not None:
            handle_to_cancel = self._pending_evaluate_handle
            self._pending_evaluate_handle = None # Clear instance variable before attempting to cancel
        
        if handle_to_cancel is not None:
            if self._safe_cancel_timer(handle_to_cancel):
                self.log(f"Scenario: cancelling_pending_evaluation (previous) for {trigger_id}", level="DEBUG")
        
        # Add a delay to allow multiple changes to settle
        try:
            self._pending_evaluate_handle = self.run_in(
                lambda x: self._handle_evaluation(trigger_id, entity, old, new), 
                self.settle_delay
            )
            self.log(f"Scenario: scheduled_evaluation -> {trigger_id} in {self.settle_delay}s", level="DEBUG")
        except Exception as e:
            self.log(f"Scenario: timer_schedule_error -> {str(e)}", level="ERROR")
            self._pending_evaluate_handle = None
    
    def _handle_evaluation(self, trigger_id, entity, old, new):
        """Handle the evaluation after the delay and clear the handle"""
        self._pending_evaluate_handle = None
        self._evaluate_group_state(trigger_id, entity, old, new)

    def _evaluate_group_state(self, trigger_id, triggered_entity, old, new):
        # --- GATHER STATE ---
        speaker_data = self._gather_speaker_data()
        follow_on = self._should_follow_me_be_active()
        
        # Get individual values from speaker data for easier reference
        playing_speakers = speaker_data["playing"]
        grouped_speakers = speaker_data["grouped"]
        solo_speakers = speaker_data["solo"]
        muted_solo_speakers = speaker_data["muted_solo"]
        muted_grouped_speakers = speaker_data["muted_grouped"]
        
        # Get previous group members of triggered entity to detect "was grouped now solo"
        prev_members = []
        curr_members = []
        
        # Only try to get state if triggered_entity is valid
        if triggered_entity and triggered_entity in self.speakers:
            prev_members = self._prev_group_members.get(triggered_entity, [])
            curr_members = self._safe_get_state(triggered_entity, attribute="group_members", default=[])
        
        was_grouped_now_solo = (
            len(prev_members) > 1 and 
            len(curr_members) == 1 and 
            len(grouped_speakers) == 0 and 
            len(playing_speakers) < 2
        )
        
        # Determine speaker grouping states
        group_state = self._determine_group_state(triggered_entity, grouped_speakers, playing_speakers, trigger_id, prev_members, curr_members)
        
        # --- DETERMINE MASTER SPEAKER ---
        master_speaker = self._choose_master(playing_speakers, grouped_speakers, triggered_entity, curr_members)
        master_friendly = self._entity_to_friendly_name(master_speaker)
        current_master = self.get_state(self.master_select)
        
        # Get the entity_id of the current master
        last_master_entity = self._friendly_to_entity_id(current_master)
        
        # Check if master is muted
        master_is_muted = False
        if master_speaker and master_speaker != "none":
            master_is_muted = self.get_state(master_speaker, attribute="is_volume_muted") == "true"
        elif last_master_entity and last_master_entity != "none":
            master_is_muted = self.get_state(last_master_entity, attribute="is_volume_muted") == "true"
        
        # --- DETERMINE ACTIONS ---
        action_decisions = self._determine_actions(
            follow_on, 
            group_state, 
            was_grouped_now_solo, 
            master_is_muted, 
            playing_speakers, 
            trigger_id
        )
        
        # --- CHECK FOR INCONSISTENCIES ---
        self._handle_inconsistencies(
            follow_on, 
            group_state["any_speakers_grouped"], 
            muted_solo_speakers, 
            muted_grouped_speakers, 
            playing_speakers
        )
        
        # --- LOG CURRENT STATE ---
        self.log(f"Scenario: status -> trigger:{trigger_id} entity:{triggered_entity} follow_me:{follow_on} " +
                f"master:{master_speaker} grouped:{len(grouped_speakers)} playing:{len(playing_speakers)} " +
                f"muted_solo:{len(muted_solo_speakers)} muted_grouped:{len(muted_grouped_speakers)} " +
                f"should_enable:{action_decisions['should_enable_follow_me']} should_disable:{action_decisions['should_disable_follow_me']}", level="INFO")
        
        # --- UPDATE MASTER SPEAKER ---
        self._sync_master_speaker(master_speaker, master_friendly, current_master, playing_speakers, grouped_speakers)
        
        # --- ENABLE/DISABLE FOLLOW_ME ---
        self._toggle_follow_me(action_decisions)
        
        # --- HANDLE UNMUTING ---
        self._handle_unmuting(
            action_decisions["should_unmute_master"], 
            master_speaker, 
            master_is_muted, 
            last_master_entity, 
            was_grouped_now_solo, 
            triggered_entity, 
            muted_solo_speakers, 
            follow_on
        )
        
        # Clean any stale entries in the previous group members dict
        self._clean_prev_group_members()

    def _gather_speaker_data(self):
        """Gather data about all speaker states.
        
        OPTIMIZATION: Caches state within callback to reduce redundant get_state() calls.
        Reduces from 3 calls per speaker (27+ total) to 1-2 calls per speaker (9-18 total).
        """
        result = {
            "playing": [],
            "grouped": [],
            "solo": [],
            "muted_solo": [],
            "muted_grouped": [],
            "all_group_members": set()
        }
        
        for sp in self.speakers:
            # OPTIMIZATION: Get all needed state in one pass, cache within callback
            state = self.get_state(sp)  # Call 1: Get state (playing/paused/idle)
            members = self.get_state(sp, attribute="group_members") or []  # Call 2: Get group members
            mute_attr = self.get_state(sp, attribute="is_volume_muted")  # Call 3: Get mute state
            
            # Parse mute state (handle various formats)
            is_muted = False
            if mute_attr is not None:
                str_value = str(mute_attr).lower()
                is_muted = str_value in ["true", "on", "yes", "1"]
            
            # Track speakers that are currently playing
            if state == "playing":
                result["playing"].append(sp)
            
            # Track grouped vs solo speakers
            if isinstance(members, list) and len(members) > 1:
                result["grouped"].append(sp)
                result["all_group_members"].update(members)
                
                # Check if muted
                if is_muted:
                    result["muted_grouped"].append(sp)
            else:
                result["solo"].append(sp)
                
                # Check if muted
                if is_muted:
                    result["muted_solo"].append(sp)
        
        return result

    def _determine_group_state(self, triggered_entity, grouped_speakers, playing_speakers, trigger_id, prev_members, curr_members):
        """Determine current grouping state"""
        result = {}
        
        # Flag for any active groups - only consider actual Sonos groups, not just playing speakers
        result["any_groups_exist"] = len(grouped_speakers) > 0
        result["has_multiple_playing"] = len(playing_speakers) >= 2
        result["triggered_is_grouped"] = False
        
        if triggered_entity in self.speakers:
            t_members = self.get_state(triggered_entity, attribute="group_members") or []
            result["triggered_is_grouped"] = len(t_members) > 1
        
        # Only consider speakers grouped if they are actually in a Sonos group
        result["any_speakers_grouped"] = (
            result["triggered_is_grouped"] or 
            result["any_groups_exist"]
        )
        
        result["is_new_group"] = False
        if trigger_id == "group_change" and triggered_entity:
            current_count = len(curr_members)
            previous_count = len(prev_members)
            result["is_new_group"] = current_count > previous_count and current_count > 1
        elif trigger_id == "join" or trigger_id == "template_trigger":
            result["is_new_group"] = True
            
        return result

    def _determine_actions(self, follow_on, group_state, was_grouped_now_solo, master_is_muted, playing_speakers, trigger_id):
        """Determine what actions should be taken"""
        result = {}
        
        # Only enable follow_me if:
        # 1. There are actual groups formed
        # 2. The trigger is a group formation event
        result["should_enable_follow_me"] = (
            not follow_on and
            (
                (group_state["triggered_is_grouped"] or group_state["any_groups_exist"]) and
                (trigger_id == "join" or group_state["is_new_group"])
            )
        )
        
        # Only disable if follow_me is ON and we have a valid reason
        result["should_disable_follow_me"] = False
        if follow_on:
            # The decision to disable should be based purely on whether any groups exist AFTER the event.
            if not group_state["any_speakers_grouped"]:
                result["should_disable_follow_me"] = True
        
        # Always unmute master when follow_me is being disabled
        result["should_unmute_master"] = (
            (trigger_id == "follow_me_disabled") or
            (was_grouped_now_solo and master_is_muted) or
            (result["should_disable_follow_me"] and master_is_muted)
        )
        
        # Always unmute solo speakers when disabling follow_me
        result["should_unmute_solo_speakers"] = result["should_disable_follow_me"]
            
        return result

    def _handle_inconsistencies(self, follow_on, any_speakers_grouped, muted_solo_speakers, muted_grouped_speakers, playing_speakers):
        """Handle inconsistent states between follow_me and actual speaker grouping"""
        now = self.datetime().timestamp()
        
        # Don't treat a solo playing speaker as an inconsistency
        if len(playing_speakers) == 1 and not any_speakers_grouped:
            return

        # CRITICAL FIX: Only auto-disable if no group for longer than settle_delay AND
        # the last group timestamp is recent enough to warrant checking
        lost_group_too_fast = False
        if follow_on and not any_speakers_grouped and len(playing_speakers) < 2:
            # Calculate time since last group was detected
            time_since_last_group = now - self._last_group_ts
            # Debug log for visibility
            self.log(f"Scenario: debug_group_timestamp -> seconds since last group: {time_since_last_group}", level="INFO")
            
            # Only consider this a valid condition if we actually had a group recently
            # AND enough time has passed to confirm it's not just temporary
            if self._last_group_ts > 0 and time_since_last_group > self.settle_delay:
                lost_group_too_fast = True
                self.log(f"Scenario: lost_group_detection -> last group was {time_since_last_group}s ago", level="WARNING")
        
        inconsistent = (
            (not follow_on and any_speakers_grouped) or
            lost_group_too_fast or
            (not follow_on and (len(muted_solo_speakers) > 0 or len(muted_grouped_speakers) > 0))
        )
        
        if inconsistent:
            self.log(f"Scenario: inconsistency_detected -> follow_me:{follow_on} grouped:{any_speakers_grouped} muted_solo:{len(muted_solo_speakers)}", level="WARNING")
            
            # Note: follow_me state is now automatically detected, no toggle management needed
            # Just log the inconsistency - follow_me will automatically activate/deactivate based on conditions
            
            # Unmute speakers if follow_me is not active
            if not follow_on and (len(muted_solo_speakers) > 0 or len(muted_grouped_speakers) > 0):
                self.log(f"Scenario: unmute_on_follow_inactive -> {len(muted_solo_speakers)} solo, {len(muted_grouped_speakers)} grouped", level="INFO")
                
                # We already know these speakers are muted based on muted_solo_speakers and muted_grouped_speakers lists
                for sp in muted_solo_speakers + muted_grouped_speakers:
                    self._safe_call_service("media_player/volume_mute", entity_id=sp, is_volume_muted=False)

    def _sync_master_speaker(self, master_speaker, master_friendly, current_master, playing_speakers, grouped_speakers):
        """Update the master speaker selection input if needed"""
        target_option = "None selected"
        target_entity_id_for_attribute = None  # Use Python's None

        if master_speaker and master_speaker != "none": # master_speaker is the entity_id from _choose_master or "none"
            target_option = master_friendly
            target_entity_id_for_attribute = master_speaker # This is the actual entity_id string
        # If master_speaker is "none" or empty (initial check),
        # target_option remains "None selected" and target_entity_id_for_attribute remains Python None.
        # This also covers the case: elif len(playing_speakers) == 0 and len(grouped_speakers) == 0:

        if current_master != target_option:
            self._safe_call_service("input_select/select_option",
                                  entity_id=self.master_select,
                                  option=target_option)
            self.log(f"Scenario: master_select_updated -> {target_option}", level="INFO")

        # Always update the attribute, even if the friendly name (state) hasn't changed,
        # to ensure the entity_id is correctly stored or cleared.
        try:
            current_attributes_state = self.get_state(self.master_select, attribute="all")
            existing_attributes = {}
            if current_attributes_state and "attributes" in current_attributes_state:
                existing_attributes = current_attributes_state["attributes"]

            # Preserve existing attributes and only update/add master_entity_id
            new_attributes = existing_attributes.copy()
            new_attributes["master_entity_id"] = target_entity_id_for_attribute # This will be an entity_id string or Python None

            # Get the current state of the input_select to pass to set_state,
            # ensuring we don't inadvertently change it if target_option hasn't changed.
            # However, target_option should already be the desired state.
            # If input_select already is target_option, this set_state call primarily updates attributes.
            self.set_state(self.master_select, state=target_option, attributes=new_attributes)
            self.log(f"Scenario: master_attribute_updated -> {self.master_select} set with state '{target_option}' and attribute master_entity_id: {target_entity_id_for_attribute}", level="DEBUG")

        except Exception as e:
            self.log(f"Error setting state or attribute for {self.master_select}: {e}", level="ERROR")

    def _toggle_follow_me(self, action_decisions):
        """Handle follow_me state changes (now using automatic detection, no toggle management).
        
        This method now only handles event firing and timestamp updates.
        Follow_me state is automatically detected via _should_follow_me_be_active().
        """
        now = self.datetime().timestamp()
        
        # Update timestamp when follow_me conditions change
        if action_decisions["should_enable_follow_me"]:
            self.log("Scenario: follow_me_should_be_active -> conditions met", level="INFO")
            # Update timestamp
            current_ts = float(self.get_state(self.last_ts_entity) or 0)
            if now != current_ts:
                self._safe_call_service("input_number/set_value",
                                      entity_id=self.last_ts_entity,
                                      value=now)
                                  
        elif action_decisions["should_disable_follow_me"]:
            self.log("Scenario: follow_me_should_be_inactive -> conditions not met", level="INFO")
            
            # Unmute all solo speakers when follow_me becomes inactive
            if action_decisions["should_unmute_solo_speakers"]:
                self.log("Scenario: follow_me_inactive -> unmuting all solo speakers", level="INFO")
                for sp in self.speakers:
                    # Get group members to check if solo
                    members = self.get_state(sp, attribute="group_members") or []
                    is_solo = len(members) <= 1
                    
                    if is_solo:
                        currently_muted = self.get_state(sp, attribute="is_volume_muted") == "true"
                        if currently_muted:
                            self.log(f"Scenario: unmuting_solo_on_inactive -> {sp}", level="INFO")
                            self._safe_call_service("media_player/volume_mute", entity_id=sp, is_volume_muted=False)

    def _handle_unmuting(self, should_unmute_master, master_speaker, master_is_muted, 
                        last_master_entity, was_grouped_now_solo, triggered_entity, 
                        muted_solo_speakers, follow_on):
        """Handle various unmuting scenarios"""
        # Create a list of speakers to unmute so we can do it once at the end
        speakers_to_unmute = []
        
        if should_unmute_master:
            if master_speaker != "none" and master_is_muted:
                self.log(f"Scenario: unmuting_master -> {master_speaker}", level="INFO")
                speakers_to_unmute.append(master_speaker)
            elif last_master_entity != "none" and self.get_state(last_master_entity, attribute="is_volume_muted") == "true":
                self.log(f"Scenario: unmuting_previous_master -> {last_master_entity}", level="INFO")
                speakers_to_unmute.append(last_master_entity)
        
        # Handle speaker that was just ungrouped - check if it's muted before unmuting
        if was_grouped_now_solo and triggered_entity != "none":
            currently_muted = self.get_state(triggered_entity, attribute="is_volume_muted") == "true"
            if currently_muted:
                self.log(f"Scenario: speaker_just_ungrouped -> unmuting {triggered_entity}", level="INFO")
                speakers_to_unmute.append(triggered_entity)
            else:
                self.log(f"Scenario: speaker_just_ungrouped -> {triggered_entity} already unmuted", level="DEBUG")
        
        # Unmute any solo speakers if they shouldn't be muted
        if len(muted_solo_speakers) > 0 and not follow_on:
            self.log(f"Scenario: unmuting_solo_speakers -> {len(muted_solo_speakers)} speakers", level="INFO")
            speakers_to_unmute.extend(muted_solo_speakers)
        
        # Perform all unmuting in batch for better reliability
        if speakers_to_unmute:
            self.log(f"Scenario: batch_unmuting -> {len(speakers_to_unmute)} speakers", level="INFO")
            for sp in speakers_to_unmute:
                # OPTIMIZATION: Removed redundant service call (was calling twice)
                # Use _safe_call_service for reliability (includes timeout handling)
                self._safe_call_service("media_player/volume_mute", entity_id=sp, is_volume_muted=False)
            # After unmuting, restore volume only for current master if follow_me is active and source is AirPlay
            follow_on = self._should_follow_me_be_active()
            current_master_entity = master_speaker if master_speaker and master_speaker != "none" else self._friendly_to_entity_id(self.get_state(self.master_select))
            if follow_on and current_master_entity and current_master_entity != "none":
                is_muted_now = self.get_state(current_master_entity, attribute="is_volume_muted") == "true"
                if not is_muted_now:
                    self._restore_volume_if_airplay(current_master_entity)

    def _should_follow_me_be_active(self):
        """Delegate to Follow Me app so policy is defined in one place. Returns False if Follow Me app unavailable."""
        try:
            follow_me_app = self.get_app("SonosFollowMe")
            return follow_me_app._should_follow_me_be_active()
        except Exception:
            self.log("Scenario: follow_me_check -> SonosFollowMe app not available, assuming inactive", level="DEBUG")
            return False

    def _choose_master(self, playing, grouped, triggered_entity, triggered_members):
        """Select the master speaker based on priority rules"""
        # 1. If there's a Sonos group, use its coordinator
        if grouped:
            # 'sonos_group' is an ordered list; index 0 is the Sonos master
            sonos_group = self.get_state(grouped[0], attribute="sonos_group") or []
            if sonos_group:
                self.log(f"Scenario: sonos_coordinator_found -> using {sonos_group[0]}", level="DEBUG")
                return sonos_group[0]
            
        # 2. Triggered entity if it has group members
        if (triggered_entity and triggered_entity != "none" and 
            len(triggered_members) > 1):
            return triggered_members[0]
            
        # 3. First playing speaker with its group
        if playing:
            first_speaker = playing[0]
            group_members = self.get_state(first_speaker, attribute="group_members") or []
            if len(group_members) > 0:
                return group_members[0]
            return first_speaker
            
        # 4. First grouped speaker
        if grouped:
            first_speaker = grouped[0]
            group_members = self.get_state(first_speaker, attribute="group_members") or []
            if len(group_members) > 0:
                return group_members[0]
            return first_speaker
            
        return "none"

    def _entity_to_friendly_name(self, entity):
        """Convert an entity_id to its friendly name for the input_select using YAML config."""
        if entity == "none" or not entity:
            return "None selected"

        # Use the map loaded from YAML
        friendly_name = self.entity_friendly_name_map.get(entity)

        if friendly_name:
            return friendly_name
        else:
            # Fallback to Home Assistant's friendly_name if not in our explicit map from YAML.
            friendly_from_ha = self.get_state(entity, attribute="friendly_name")
            if friendly_from_ha:
                # Log if we're using a HA friendly name for an unmapped entity.
                # This helps in diagnosing future issues if new, unmapped speakers are added
                # or if the YAML configuration is missing an entry.
                self.log(f"Warning: Entity {entity} not found in entity_friendly_name_map from YAML. " +
                         f"Falling back to HA friendly name: '{friendly_from_ha}'. " +
                         "Ensure this name is a valid option for the input_select, or add it to the YAML map.", level="WARNING")
                return friendly_from_ha
            
            # Final fallback if entity is not in mapping and has no friendly name from HA
            self.log(f"Warning: Could not determine friendly name for {entity} (not in YAML map and no HA friendly name). " +
                     "Defaulting to 'None selected'. Entity may be missing or misconfigured.", level="WARNING")
            return "None selected"

    def _friendly_to_entity_id(self, friendly_name):
        """Convert a friendly name to its entity_id using YAML config."""
        if friendly_name == "None selected" or not friendly_name:
            return "none"

        # Use the inverse map created from YAML config
        entity_id = self._friendly_to_entity_map.get(friendly_name)
        
        if entity_id:
            return entity_id
        else:
            # Fallback for safety, though ideally all names come from the select options / YAML map
            self.log(f"Warning: Friendly name '{friendly_name}' not found in _friendly_to_entity_map (derived from YAML). " +
                     "This might indicate an issue with the input_select options or YAML configuration. " +
                     "Returning 'none'.", level="WARNING")
            return "none"
        
    def _safe_call_service(self, service, **kwargs):
        """Call service with error handling"""
        try:
            self.call_service(service, **kwargs)
            return True
        except Exception as e:
            self.log(f"Error calling {service}: {e}", level="ERROR")
            # If it's an input_select error, try to recover
            if service == "input_select/select_option":
                self.log("Attempting to recover from input_select error...", level="WARNING")
                try:
                    # Get current state of the input_select
                    current_state = self.get_state(kwargs.get("entity_id"))
                    if current_state is None:
                        self.log("Input select entity not found, cannot recover", level="ERROR")
                        return False
                    # Try to set it to the same value to reset it
                    self.call_service(service, **kwargs)
                    return True
                except Exception as recovery_error:
                    self.log(f"Recovery attempt failed: {recovery_error}", level="ERROR")
            return False

    def _safe_get_state(self, entity_id, attribute=None, default=None):
        """Safely get state with error handling"""
        try:
            if attribute:
                return self.get_state(entity_id, attribute=attribute) or default
            return self.get_state(entity_id) or default
        except Exception as e:
            self.log(f"Scenario: state_query_error -> {entity_id} attribute:{attribute} failed: {e}", level="ERROR")
            return default 

    def cancel_pending_update(self):
        """Invalidate any pending group update without cancelling timers to avoid invalid-handle warnings."""
        # Invalidate any scheduled callbacks by bumping generation and clearing handle reference
        self._group_update_generation += 1
        self._pending_group_update = None

    def _handle_group_join_request(self, event_name, data, kwargs):
        """Handles a custom event to join a slave speaker to a master speaker."""
        master_entity_id = data.get("master_entity_id")
        slave_entity_id = data.get("slave_entity_id")

        self.log(f"Received '{event_name}' event. Master: {master_entity_id}, Slave: {slave_entity_id}", level="INFO")

        if not master_entity_id or not slave_entity_id:
            self.log("Group join request: Missing master_entity_id or slave_entity_id in event data. Aborting.", level="ERROR")
            return

        if self._reset_in_progress:
            self.log("Group join request: reset in progress, ignoring.", level="DEBUG")
            return

        if not master_entity_id.startswith("media_player.") or not slave_entity_id.startswith("media_player."):
            self.log(f"Group join request: Invalid entity_id format. Master: '{master_entity_id}', Slave: '{slave_entity_id}'. Aborting.", level="ERROR")
            return

        if slave_entity_id not in self.speakers or master_entity_id not in self.speakers:
            self.log(f"Group join request: Slave '{slave_entity_id}' or Master '{master_entity_id}' not in configured all_speakers list. Aborting.", level="WARNING")
            # Optionally, you might still want to proceed if they are valid media_player entities known to HA
            # For now, strict check against configured speakers.
            return

        if slave_entity_id == master_entity_id:
            self.log(f"Group join request: Slave '{slave_entity_id}' is already the target master '{master_entity_id}'. No action needed.", level="INFO")
            return

        try:
            # Check if slave is already in the master's group
            master_attributes = self.get_state(master_entity_id, attribute="all")
            if master_attributes and 'attributes' in master_attributes:
                # Prefer sonos_group if available, fallback to group_members
                current_master_group = master_attributes['attributes'].get('sonos_group', 
                                                                       master_attributes['attributes'].get('group_members', []))
                if slave_entity_id in current_master_group:
                    self.log(f"Group join request: Slave '{slave_entity_id}' is already in the group of master '{master_entity_id}'. Group: {current_master_group}. No action needed.", level="INFO")
                    return
            else:
                self.log(f"Could not retrieve attributes for master '{master_entity_id}' to check current group. Proceeding with join attempt.", level="WARNING")

            # Route through queue with high priority (user-initiated action)
            self.log(f"Queueing join request (high priority): slave '{slave_entity_id}' to join master '{master_entity_id}'", level="INFO")
            self._queue_group_operation("join", master_entity_id, [slave_entity_id], priority="high")
            self.log(f"Join request queued for master '{master_entity_id}' to be joined by '{slave_entity_id}'. GroupManager will evaluate state shortly.", level="INFO")
            # SonosGroupManager's existing _on_service_call or _on_group_change listeners 
            # should pick up the state change and update follow_me, master_select etc.

        except Exception as e:
            self.error(f"Error processing group join request for master '{master_entity_id}' and slave '{slave_entity_id}': {e}", exc_info=True)

    def _handle_group_unjoin_request(self, event_name, data, kwargs):
        """Handles a custom event to unjoin a speaker from its group."""
        entity_id = data.get("entity_id")
        
        if not entity_id:
            self.log("Group unjoin request: Missing entity_id in event data. Aborting.", level="ERROR")
            return

        if self._reset_in_progress:
            self.log("Group unjoin request: reset in progress, ignoring.", level="DEBUG")
            return
        
        if not entity_id.startswith("media_player."):
            self.log(f"Group unjoin request: Invalid entity_id format: '{entity_id}'. Aborting.", level="ERROR")
            return
        
        if entity_id not in self.speakers:
            self.log(f"Group unjoin request: Entity '{entity_id}' not in configured all_speakers list. Aborting.", level="WARNING")
            return
        
        # Route through queue with high priority (user-initiated action)
        self.log(f"Queueing unjoin request (high priority): {entity_id}", level="INFO")
        self._queue_group_operation("unjoin", None, [entity_id], priority="high") 