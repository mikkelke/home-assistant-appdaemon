# /conf/apps/sonos/state_reset.py

import appdaemon.plugins.hass.hassapi as hass  # type: ignore 

class SonosStateReset(hass.Hass):
    """
    AppDaemon port of the "Sonos state reset" automation:

    Sonos grouping matches the Control API model (players always in a group; transport and
    group edits target the group; coordinator leads the group - see
    https://docs.sonos.com/docs/control). Music Assistant drives Sonos via aiosonos
    (local websocket), where ``SonosGroup`` exposes ``coordinator_id`` and ``player_ids``;
    Home Assistant surfaces the same membership as ``media_player`` attributes, mainly
    ``group_members`` (entity IDs). Core Sonos may also set ``sonos_group`` (ordered,
    coordinator-first). MA aligns ``group_members`` with other integrations (coordinator
    listed first on the leader entity). This app never calls Sonos or MA APIs directly; it
    only uses HA ``media_player`` services. Use ``_ha_sonos_group_entity_ids`` for the
    member list and ``_ha_sonos_coordinator_entity_id`` for the coordinator entity.

    1) Volume Management:
       - Restores speaker volumes to configured levels
       - Handles volume state during resets
    2) Inactivity Handling:
       - Monitors speaker inactivity states
       - Triggers auto-reset after configurable timeout
    3) Reset Operations:
       - Coordinates with GroupManager for safe resets
       - Handles manual reset requests
       - Manages speaker state during resets
    4) Cleanup:
       - Unjoins speakers from groups
       - Unmutes speakers
       - Restores default states
    5) Immediate safety (same rule as follow_me):
       - As soon as a speaker is not playing, clear mute (do not wait for inactivity timer / full reset).
       - Full reset still runs later for volume + ungroup.
    """

    def initialize(self):
        self.speakers          = self.args["all_speakers"]
        self.follow_flag       = self.args["follow_me_flag"]
        self.inactivity_sec    = int(self.args["inactivity_seconds"])
        self.speaker_volumes   = self.args["speaker_volumes"]
        self.default_volume    = float(self.args["default_volume"])
        # New: controls to avoid blocking callbacks on slow Sonos ungroup operations
        self.ungroup_settle_sec   = int(self.args.get("ungroup_settle_seconds", 8))      # min wait before first solo poll
        self.unjoin_strategy      = self._resolve_unjoin_strategy()
        self.reset_solo_poll_sec  = float(self.args.get("reset_solo_poll_seconds", 1.0))
        self.reset_solo_max_polls = int(self.args.get("reset_solo_max_poll_attempts", 15))
        self._reset_in_progress = False  # Track if a reset is in progress
        self._inactivity_timers = {}  # Track inactivity timers for each speaker
        # Generation counters to invalidate stale timers without cancelling (avoids invalid-handle warnings)
        self._inactivity_generation = {}
        self._active_reset_ctx = None  # Context dict while phased reset runs

        # Debug log initialization
        self.log(f"SonosStateReset initialized with {len(self.speakers)} speakers", level="INFO")
        self.log(f"Inactivity timeout set to {self.inactivity_sec} seconds", level="INFO")
        self.log(f"Follow me flag: {self.follow_flag}", level="INFO")
        self.log(f"reset unjoin_strategy={self.unjoin_strategy}", level="INFO")

        # listen for manual event
        self.listen_event(self._manual_reset, "sonos_reset_all")
        self.log("Listening for sonos_reset_all events", level="INFO")

        # listen for targeted manual reset of specific speakers
        self.listen_event(self._manual_reset_targets, "sonos_reset_speakers")
        self.log("Listening for sonos_reset_speakers events", level="INFO")

        # Listen for Group Manager ready signal; only then execute reset (event-driven, not time-based)
        self.listen_event(self._on_reset_ready, "sonos_reset_ready")
        self.log("Listening for sonos_reset_ready (from Group Manager)", level="INFO")

        # for each speaker: monitor state changes and group changes for inactivity timers
        for sp in self.speakers:
            self.listen_state(self._on_speaker_state_change, sp)
            self.listen_state(self._on_speaker_group_change, sp, attribute="group_members")
            self.listen_state(self._on_speaker_group_change, sp, attribute="sonos_group")
            self.log(f"Monitoring {sp} for state and group changes", level="INFO")

        # listen_state only fires on transitions - speakers already paused/idle at load would never get a timer
        self.run_in(self._bootstrap_inactivity_timers, 15)

        self.log("SonosStateReset loaded and monitoring", level="INFO")

    def _resolve_unjoin_strategy(self):
        """coordinator_only | members_except_coordinator | all_members. Legacy: skip_coordinator_unjoin."""
        valid = frozenset({"coordinator_only", "members_except_coordinator", "all_members"})
        raw = self.args.get("unjoin_strategy")
        if raw is not None:
            s = str(raw).strip().lower().replace("-", "_")
            if s in valid:
                return s
            self.log(f"Unknown unjoin_strategy {raw!r}, using all_members", level="WARNING")
            return "all_members"
        if "skip_coordinator_unjoin" in self.args:
            return (
                "members_except_coordinator"
                if bool(self.args.get("skip_coordinator_unjoin"))
                else "all_members"
            )
        # Default all_members: matches HA Sonos batching (coordinators unjoined last in unjoin_multi)
        return "all_members"

    def _ha_sonos_group_entity_ids(self, entity_id):
        """Return this media_player's group member entity_ids (maps to MA SonosGroup.player_ids).

        Under Music Assistant, members come from ``group_members``. Under core Sonos,
        ``sonos_group`` is often set with coordinator-first ordering (same role as
        ``coordinator_id`` in aiosonos). Precedence here: use a multi-member ``sonos_group``
        if present, else multi-member ``group_members``; then single-element lists for
        solo; ``[]`` if unknown. Malformed values are treated as solo.
        """
        try:
            raw_sg = self.get_state(entity_id, attribute="sonos_group")
            raw_gm = self.get_state(entity_id, attribute="group_members")
            sg = raw_sg if isinstance(raw_sg, list) else []
            gm = raw_gm if isinstance(raw_gm, list) else []
            if len(sg) > 1:
                return list(sg)
            if len(gm) > 1:
                return list(gm)
            if len(sg) == 1:
                return list(sg)
            if len(gm) == 1:
                return list(gm)
            return []
        except Exception:
            return []

    def _ha_sonos_coordinator_entity_id(self, entity_id):
        """HA entity_id for the group coordinator (MA SonosGroup.coordinator_id at the HA layer).

        When ``len(members) > 1``, returns ``members[0]`` (coordinator-first ``group_members``
        / ``sonos_group``). For solo (at most one member in the list) or ``[]``, returns
        ``entity_id`` so inactivity timers stay keyed to the speaker that reported state.
        """
        members = self._ha_sonos_group_entity_ids(entity_id)
        if len(members) > 1:
            return members[0]
        return entity_id

    def _group_list_for_unjoin(self, sp):
        """Multi-member group list for unjoin, or None if solo / empty / malformed."""
        grp = self._ha_sonos_group_entity_ids(sp)
        if len(grp) <= 1:
            return None
        return grp

    def _build_unjoin_queue(self, working, speaker_set):
        """
        Build ordered unjoin entity list with deduplication.
        coordinator_only: one unjoin per group (coordinator only).
        members_except_coordinator: all members except first/coordinator.
        all_members: every member in the group (known speakers), deduped - best match to HA
        Sonos async_unjoin_player coalescing + unjoin_multi (coordinators removed last).
        """
        to_unjoin = []
        seen = set()
        coordinators_seen = set()

        for sp in working:
            if not self._is_grouped(sp):
                continue
            grp = self._group_list_for_unjoin(sp)
            if grp is None:
                self.log(
                    f"reset_phase_unjoin: skip {sp} - empty or malformed group (_ha_sonos_group_entity_ids)",
                    level="DEBUG",
                )
                continue
            coordinator = self._ha_sonos_coordinator_entity_id(sp)
            if not coordinator:
                self.log(f"reset_phase_unjoin: skip {sp} - no coordinator in group list", level="DEBUG")
                continue

            queued_here = False
            if self.unjoin_strategy == "coordinator_only":
                if coordinator not in speaker_set:
                    self.log(
                        f"reset_phase_unjoin: skip group via {sp} - coordinator {coordinator} not in all_speakers",
                        level="DEBUG",
                    )
                    continue
                if coordinator in seen:
                    continue
                seen.add(coordinator)
                to_unjoin.append(coordinator)
                queued_here = True
            elif self.unjoin_strategy == "members_except_coordinator":
                for m in grp:
                    if m not in speaker_set or m in seen:
                        continue
                    if m == coordinator:
                        continue
                    seen.add(m)
                    to_unjoin.append(m)
                    queued_here = True
            else:  # all_members
                for m in grp:
                    if m not in speaker_set or m in seen:
                        continue
                    seen.add(m)
                    to_unjoin.append(m)
                    queued_here = True
            if queued_here:
                coordinators_seen.add(coordinator)

        return to_unjoin, sorted(coordinators_seen)

    def _execute_unjoin_batch(self, kwargs):
        """Issue unjoin for each entity in one callback, back-to-back.

        Home Assistant Sonos coalesces unjoin service calls within ~0.1s per household
        and runs unjoin_multi(), removing coordinators last. Staggering unjoins by
        hundreds of ms splits batches and defeats that optimization.
        """
        if not isinstance(kwargs, dict):
            kwargs = {}
        entities = kwargs.get("entities") or []
        if not entities:
            return
        self.log(
            f"reset_phase_unjoin: batch {len(entities)} unjoin(s) in one shot for HA coalesce/unjoin_multi",
            level="INFO",
        )
        for eid in entities:
            self._safe_call_service("media_player/unjoin", entity_id=eid)

    def _safe_cancel_timer(self, timer_handle):
        """Safely cancel a timer without triggering invalid-handle warnings."""
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

    def _parse_muted(self, val):
        if val is None:
            return False
        return str(val).lower() in ("true", "on", "yes", "1")

    def _is_muted(self, entity_id):
        return self._parse_muted(self.get_state(entity_id, attribute="is_volume_muted"))

    def _on_speaker_state_change(self, entity, attribute, old, new, kwargs):
        """Handle speaker state changes: immediate unmute when not playing, then inactivity timers."""
        self.log(f"Speaker {entity} state transition: {old} -> {new}", level="INFO")

        if new == "unavailable":
            self.log(f"Speaker {entity} is unavailable - skipping timer", level="DEBUG")
            return

        # Same strict rule as SonosFollowMe: mute only applies while playing. Do not wait for
        # inactivity_seconds + Group Manager reset - those can fail or be too late.
        if new is not None:
            ns = str(new).lower()
            if ns not in ("unavailable", "unknown") and new != "playing":
                if self._is_muted(entity) and not self._reset_in_progress:
                    self.log(
                        f"Speaker {entity} state={new} but muted -> immediate unmute (state_reset safety net)",
                        level="INFO",
                    )
                    self._safe_call_service(
                        "media_player/volume_mute",
                        entity_id=entity,
                        is_volume_muted=False,
                    )

        if new in ["paused", "idle", "standby"]:
            # One timer per group, keyed by coordinator (solo: entity is its own coordinator).
            # Any group member can go paused/idle; we must arm the timer on the coordinator.
            # Previously only the coordinator's own transition called _start_inactivity_timer, so
            # slaves never started a countdown - volume could stay high indefinitely after a party.
            timer_entity = self._get_timer_entity(entity)
            self._invalidate_timer_for(timer_entity)
            self._start_inactivity_timer(timer_entity, new)
        else:
            self._invalidate_timer_for(entity)
            self.log(f"Speaker {entity} is {new} - no timer needed", level="DEBUG")

    def _on_speaker_group_change(self, entity, attribute, old, new, kwargs):
        """When a speaker is removed from a group (becomes solo) and is paused/idle, start inactivity timer so it gets reset."""
        old_members = old if isinstance(old, list) else []
        new_members = new if isinstance(new, list) else []
        was_grouped = len(old_members) > 1
        now_solo = len(new_members) <= 1

        if was_grouped and now_solo:
            state = self.get_state(entity)
            # Backup unmute if still muted while not playing (follow_me / GM may have missed edge cases)
            if state not in ("playing", "unavailable", None) and str(state).lower() != "unknown":
                if self._is_muted(entity) and not self._reset_in_progress:
                    self.log(
                        f"Speaker {entity} left group solo state={state} but muted -> immediate unmute (state_reset safety net)",
                        level="INFO",
                    )
                    self._safe_call_service(
                        "media_player/volume_mute",
                        entity_id=entity,
                        is_volume_muted=False,
                    )
            if state in ["paused", "idle", "standby"]:
                te = self._get_timer_entity(entity)
                self._invalidate_timer_for(te)
                self._start_inactivity_timer(te, state)
                self.log(f"Speaker {entity} left group (now solo, state={state}); started inactivity timer for reset", level="INFO")
        elif now_solo and was_grouped is False:
            pass
        else:
            # Group joined or reshuffled; timers are keyed by coordinator, not member entity_id.
            tk = self._get_timer_entity(entity)
            if tk in self._inactivity_timers:
                self._invalidate_timer_for(entity)
                self.log(f"Speaker {entity} group changed; cleared inactivity timer for {tk}", level="DEBUG")

    def _get_timer_entity(self, entity):
        """Return the entity that owns the inactivity timer: coordinator if grouped, else this entity."""
        return self._ha_sonos_coordinator_entity_id(entity)

    def _invalidate_timer_for(self, entity):
        """Invalidate any existing timer for this entity (and if in a group, for the coordinator)."""
        timer_key = self._get_timer_entity(entity)
        self._inactivity_generation[timer_key] = self._inactivity_generation.get(timer_key, 0) + 1
        if timer_key in self._inactivity_timers:
            del self._inactivity_timers[timer_key]

    def _start_inactivity_timer(self, timer_entity, state):
        """Start inactivity timer keyed by group coordinator (or solo speaker). Caller must pass that id."""
        if self.get_state(timer_entity) == "unavailable":
            return
        if timer_entity in self._inactivity_timers:
            return
        try:
            current_gen = self._inactivity_generation.get(timer_entity, 0)
            self._inactivity_timers[timer_entity] = self.run_in(
                self._auto_reset,
                self.inactivity_sec,
                entity=timer_entity,
                state=state,
                gen=current_gen
            )
            self.log(f"Starting {self.inactivity_sec}s inactivity timer for {timer_entity} (state={state})", level="INFO")
        except Exception as e:
            self.log(f"Error starting timer for {timer_entity}: {e}", level="ERROR")
            self._inactivity_timers[timer_entity] = None

    def _bootstrap_inactivity_timers(self, kwargs=None):
        """Start inactivity timers for speakers already paused/idle/standby when the app loads.

        Without this, `listen_state` never runs for entities that did not transition after startup,
        so they would not auto-reset until the next play/pause cycle.
        """
        if self._reset_in_progress:
            return
        seen_te = set()
        for sp in self.speakers:
            try:
                timer_entity = self._get_timer_entity(sp)
            except Exception:
                continue
            if timer_entity in seen_te:
                continue
            try:
                coord_state = self.get_state(timer_entity)
            except Exception:
                continue
            if coord_state == "unavailable":
                continue
            if coord_state not in ("paused", "idle", "standby"):
                continue
            if timer_entity in self._inactivity_timers:
                continue
            seen_te.add(timer_entity)
            self.log(
                f"reset_bootstrap_inactivity -> {timer_entity} already {coord_state} (seen via {sp}), starting timer (no transition since app load)",
                level="INFO",
            )
            self._start_inactivity_timer(timer_entity, coord_state)

    def _manual_reset(self, event_name, data, kwargs):
        """Triggered by event_type=sonos_reset_all"""
        if self._reset_in_progress:
            self.log("Scenario: reset_already_in_progress -> skipping duplicate reset request", level="WARNING")
            return
        self.log(f"Received manual reset event: {event_name}", level="INFO")
        self._do_reset(self.speakers, trigger="manual_trigger")

    def _manual_reset_targets(self, event_name, data, kwargs):
        """Triggered by event_type=sonos_reset_speakers with a list of targets."""
        if self._reset_in_progress:
            self.log("Scenario: reset_already_in_progress -> skipping targeted reset request", level="WARNING")
            return

        targets = data.get("targets") if isinstance(data, dict) else None
        if not targets or not isinstance(targets, list):
            self.log("Invalid or missing 'targets' for sonos_reset_speakers event; expected list of entity_ids", level="ERROR")
            return

        # Filter only known speakers to avoid unexpected calls
        valid_targets = [sp for sp in targets if sp in self.speakers]
        if not valid_targets:
            self.log(f"No valid targets to reset from provided list: {targets}", level="WARNING")
            return

        self.log(f"Received targeted reset event for: {', '.join(valid_targets)}", level="INFO")
        # Optional human-readable origin ("Living room TV turned on") - flows through to the
        # house-activity entry so a requested reset can say WHO wanted it, not "(manual_trigger)".
        req_source = data.get("source") if isinstance(data, dict) else None
        self._do_reset(valid_targets, trigger="manual_trigger", source=req_source)

    def _auto_reset(self, kwargs):
        """Called when inactivity timer expires"""
        entity = kwargs.get("entity")
        state = kwargs.get("state")
        gen = kwargs.get("gen")
        # If this callback is stale (generation changed), ignore
        if gen is not None and gen != self._inactivity_generation.get(entity):
            return
        
        # Only run reset if still not playing (paused/idle/standby/etc.)
        current_state = self.get_state(entity)
        if current_state == "playing":
            self.log(f"Speaker {entity} is now playing - skipping auto-reset", level="DEBUG")
            return

        # Prevent auto-reset if a manual reset is in progress
        if self._reset_in_progress:
            self.log(f"Reset already in progress - skipping auto-reset for {entity}", level="DEBUG")
            return
            
        self.log(f"Auto-reset triggered for {entity} after {self.inactivity_sec}s in {state} state", level="INFO")
        if entity in self._inactivity_timers:
            del self._inactivity_timers[entity]

        group = self._ha_sonos_group_entity_ids(entity)

        if len(group) > 1:
            self.log(f"Group pause detected, reset list (unjoin in execute): {group}", level="INFO")
            self._do_reset(group, trigger="auto_trigger", source=entity)
        else:
            self._do_reset([entity], trigger="auto_trigger", source=entity)

    def _is_grouped(self, entity_id):
        """Return True if the speaker is currently in a Sonos group (len > 1)."""
        return len(self._ha_sonos_group_entity_ids(entity_id)) > 1

    def _speaker_is_solo(self, entity_id):
        """True when entity has at most one member in its group (solo or unknown/unavailable)."""
        try:
            st = self.get_state(entity_id)
            if st in ("unavailable", "unknown") or (st is not None and str(st).lower() == "unknown"):
                return True
            return len(self._ha_sonos_group_entity_ids(entity_id)) <= 1
        except Exception:
            return True

    def _all_targets_solo(self, entity_ids):
        return all(self._speaker_is_solo(eid) for eid in entity_ids)

    def _do_reset(self, targets, trigger, source=None):
        if trigger == "manual_trigger":
            msg = f"Manual reset. Reset list: {', '.join(targets)}"
        else:
            msg = (f"Auto-reset after inactivity on {source}, "
                   f"reset list: {', '.join(targets)}")
        
        self.log(msg, level="INFO")
        self._reset_in_progress = True
        self.log("Waiting for Group Manager to pause (listening for sonos_reset_ready)...", level="INFO")
        self.fire_event("sonos_reset_requested", 
                       targets=targets,
                       trigger=trigger,
                       source=source)

    def _on_reset_ready(self, event_name, data, kwargs):
        """Run when Group Manager has paused and fired sonos_reset_ready. Execute reset with event data."""
        payload = data or {}
        if not payload.get("targets"):
            self.log("sonos_reset_ready received with no targets, skipping execute", level="WARNING")
            self._reset_in_progress = False
            return
        self.log("Received sonos_reset_ready, executing reset", level="INFO")
        self._execute_reset(payload)

    def _execute_reset(self, kwargs):
        """Phased reset: started -> unjoin -> poll solo -> volume/mute -> completed."""
        targets = kwargs["targets"]
        trigger = kwargs["trigger"]
        source = kwargs.get("source")
        speaker_set = set(self.speakers)
        poll_entities = sorted({t for t in targets if t in speaker_set})

        try:
            self.fire_event("sonos_reset_started", 
                           targets=targets,
                           trigger=trigger,
                           source=source)

            self.log("Scenario: reset_follow_me_coordination -> follow_me pauses via sonos_reset_started", level="INFO")

            if trigger == "auto_trigger":
                working = [sp for sp in poll_entities if self.get_state(sp) != "playing"]
            else:
                working = []
                for sp in poll_entities:
                    st = self.get_state(sp)
                    if st in ("unavailable", "unknown") or (st is not None and str(st).lower() == "unknown"):
                        continue
                    working.append(sp)

            if not working and trigger == "auto_trigger":
                self.log("reset: all targets playing (auto), nothing to reset", level="INFO")
                self._finish_reset(list(targets), trigger, source)
                return

            if not working:
                self.log("reset_phase_unjoin: no valid manual targets", level="WARNING")
                self._finish_reset(list(targets), trigger, source)
                return

            to_unjoin, coordinators_for_log = self._build_unjoin_queue(working, speaker_set)
            self.log(
                f"reset_phase_unjoin: strategy={self.unjoin_strategy} "
                f"coordinators={coordinators_for_log} queue={to_unjoin} working={working}",
                level="INFO",
            )

            self._active_reset_ctx = {
                "trigger": trigger,
                "source": source,
                "original_targets": list(targets),
                "poll_entities": poll_entities,
                "poll_n": 0,
                "max_poll_attempts": self.reset_solo_max_polls,
                "poll_interval": self.reset_solo_poll_sec,
            }

            if not to_unjoin:
                self._begin_finalize_reset(max(1.0, float(self.ungroup_settle_sec)))
                return

            # Single timer: all unjoins in one callback so HA can coalesce (~100ms window) and
            # run unjoin_multi with coordinators last - do not stagger across hundreds of ms.
            self.run_in(self._execute_unjoin_batch, 0, entities=list(to_unjoin))

            first_poll_delay = max(1.0, float(self.ungroup_settle_sec))
            self._begin_finalize_reset(first_poll_delay)

        except Exception as e:
            self.log(f"reset_phase_error -> {e}", level="ERROR")
            self._finish_reset(list(targets), trigger, source)

    def _begin_finalize_reset(self, delay_seconds):
        """Schedule first solo poll after optional wait."""
        self.log(f"reset_phase_poll: first check in {delay_seconds:.1f}s", level="INFO")
        self.run_in(self._finalize_reset_poll_step, delay_seconds)

    def _finalize_reset_poll_step(self, kwargs=None):
        ctx = self._active_reset_ctx
        if not ctx:
            self.log("reset_phase_poll: no active ctx, abort step", level="WARNING")
            return

        ctx["poll_n"] = ctx.get("poll_n", 0) + 1
        poll_entities = ctx["poll_entities"]
        solo = self._all_targets_solo(poll_entities)

        if solo:
            self.log(
                f"reset_phase_finalize: all solo after poll attempt {ctx['poll_n']}/{ctx['max_poll_attempts']}",
                level="INFO",
            )
            self._apply_reset_volume_mute(ctx)
            self._finish_reset(ctx["original_targets"], ctx["trigger"], ctx["source"])
            return

        if ctx["poll_n"] >= ctx["max_poll_attempts"]:
            self.log(
                f"reset_phase_poll: timeout after {ctx['poll_n']} attempts, finalizing anyway",
                level="WARNING",
            )
            self._apply_reset_volume_mute(ctx)
            self._finish_reset(ctx["original_targets"], ctx["trigger"], ctx["source"])
            return

        self.log(
            f"reset_phase_poll: attempt {ctx['poll_n']}/{ctx['max_poll_attempts']} not all solo yet",
            level="DEBUG",
        )
        self.run_in(self._finalize_reset_poll_step, ctx["poll_interval"])

    def _apply_reset_volume_mute(self, ctx):
        trigger = ctx["trigger"]
        for sp in ctx["poll_entities"]:
            st = self.get_state(sp)
            if trigger == "auto_trigger" and st == "playing":
                self.log(f"reset_finalize_skip_playing -> {sp}", level="DEBUG")
                continue
            if trigger == "manual_trigger":
                if st in ("unavailable", "unknown") or (st is not None and str(st).lower() == "unknown"):
                    continue
            vol = self.speaker_volumes.get(sp, self.default_volume)
            self._safe_call_service(
                "media_player/volume_set",
                entity_id=sp,
                volume_level=vol
            )
            self._safe_call_service(
                "media_player/volume_mute",
                entity_id=sp,
                is_volume_muted=False
            )
            self.log(f"reset_finalize_vol_mute -> {sp} vol={vol}", level="INFO")

    def _finish_reset(self, targets, trigger, source):
        """Fire sonos_reset_completed and clear local reset state."""
        try:
            self.fire_event("sonos_reset_completed",
                           targets=targets,
                           trigger=trigger,
                           source=source)
            # Explain the invisible cleanup to the dashboard's Home activity feed
            # (admin audience - housekeeping Mikkel cares about, housemates don't).
            # Names, not counts: two resets minutes apart ("living room speaker after the
            # TV grabbed it" / "rooftop speaker idle 5 min") read as a duplicate-event bug
            # when both just say "1 speaker" (user hit exactly this 2026-07-16 20:12/20:15).
            try:
                rooms = [self._speaker_room_label(t) for t in targets] if isinstance(targets, (list, tuple)) else []
                if len(rooms) > 3:
                    rooms_txt = ", ".join(rooms[:3]) + f" + {len(rooms) - 3} more"
                else:
                    rooms_txt = ", ".join(rooms) if rooms else "Speaker"
                plural_s = "s" if len(rooms) != 1 else ""
                if trigger == "auto_trigger":
                    # source here is the idle entity itself, not a human string - say the rule.
                    cause = f"{rooms_txt} speaker{plural_s} idle for {int(self.inactivity_sec // 60)} min"
                elif isinstance(source, str) and source.strip() and not source.startswith("media_player."):
                    cause = source.strip()[:120]
                else:
                    cause = "Speaker reset requested"
                self.fire_event(
                    "house_events_report",
                    cause=cause,
                    effect=f"{rooms_txt} speaker{plural_s} back to default volume and ungrouped",
                    icon="mdi:speaker-multiple",
                    audience="admin",
                )
            except Exception:
                pass
        finally:
            self._active_reset_ctx = None
            self._reset_in_progress = False

    @staticmethod
    def _speaker_room_label(entity):
        """media_player.living_room -> 'Living room' (for the feed entry)."""
        name = str(entity).split(".", 1)[-1]
        return name.replace("_", " ").strip().capitalize()

    def _safe_call_service(self, service, **kwargs):
        """Call service with error handling"""
        try:
            self.call_service(service, **kwargs)
            return True
        except Exception as e:
            self.log(f"Error calling {service}: {e}", level="ERROR")
            return False

    def _test_reset(self, kwargs):
        """Test function to verify the reset functionality"""
        self.log("Running test reset...", level="INFO")
        # Fire the manual reset event
        self.fire_event("sonos_reset_all")
        self.log("Test reset event fired", level="INFO")
