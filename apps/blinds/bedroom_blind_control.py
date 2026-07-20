import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import cover_util


class BedroomBlindControl(hass.Hass):
    """
    BedroomBlindControl

    Listens for Z-Wave JS central scene "hold" events from the bedroom remote and
    sets the bedroom blind position accordingly.

    Defaults (can be overridden via YAML args):
    - property_key_close: "001" -> sets position to 100 (fully closed)
    - property_key_partial: "002" -> sets position to 38 (preferred partial open)
    - event_value_held: "KeyHeldDown"
    """

    def initialize(self):
        # Required configuration
        self.device_id = self.args.get("device_id")
        self.target_cover = self.args.get("target_cover")
        # Optional: bathroom blind for linked action
        self.bathroom_blind_entity = self.args.get("bathroom_blind_entity")

        if not self.device_id:
            self.error("'device_id' is missing in configuration. App will not listen to events.")
            return
        if not self.target_cover:
            self.error("'target_cover' is missing in configuration. App cannot control a cover.")
            return

        # Z-Wave listener configuration
        self.zwave_command_class = int(self.args.get("zwave_command_class", 91))
        self.zwave_endpoint = int(self.args.get("zwave_endpoint", 0))

        # Scene/property keys and event values
        self.property_key_close = str(self.args.get("property_key_close", "001"))
        self.property_key_partial = str(self.args.get("property_key_partial", "002"))
        self.event_value_held = str(self.args.get("event_value_held", "KeyHeldDown"))

        # Target positions
        self.close_position = int(self.args.get("close_position", 100))
        self.partial_position = int(self.args.get("partial_position", 38))
        self.bathroom_partial_position = int(self.args.get("bathroom_partial_position", 40))
        # How close current position must be to partial_position to consider it a partial state
        self.partial_match_tolerance = int(self.args.get("partial_match_tolerance", 2))
        # Threshold for considering a blind "closed" (handles low battery cases where blind stops at 99%)
        self.closed_threshold = int(self.args.get("closed_threshold", 95))
        # Cooldown to avoid repeated actions on continuous KeyHeldDown events
        try:
            self.hold_cooldown_seconds = float(self.args.get("hold_cooldown_seconds", 1.0))
        except Exception:
            self.hold_cooldown_seconds = 1.0
        # Delay for sequential state checking to avoid race conditions
        self.state_check_delay = float(self.args.get("state_check_delay", 0.1))
        # Delay before verifying position after service call
        self.verification_delay = float(self.args.get("verification_delay", 2.0))
        self._last_close_hold_ts = None
        self._last_partial_hold_ts = None

        # Validate configuration
        if self.close_position < 0 or self.close_position > 100:
            self.error(f"Invalid close_position: {self.close_position}. Must be 0-100.")
            return
        if self.partial_position < 0 or self.partial_position > 100:
            self.error(f"Invalid partial_position: {self.partial_position}. Must be 0-100.")
            return
        if self.bathroom_partial_position < 0 or self.bathroom_partial_position > 100:
            self.error(f"Invalid bathroom_partial_position: {self.bathroom_partial_position}. Must be 0-100.")
            return
        if self.closed_threshold < 0 or self.closed_threshold > 100:
            self.error(f"Invalid closed_threshold: {self.closed_threshold}. Must be 0-100.")
            return

        # Verify entities are available on startup
        self._verify_entities_on_startup()

        # Register listeners
        self._register_hold_listener(
            handler=self._handle_close_hold,
            property_key=self.property_key_close,
            description=f"close to {self.close_position}%",
        )
        self._register_hold_listener(
            handler=self._handle_partial_hold,
            property_key=self.property_key_partial,
            description=f"partial to {self.partial_position}%",
        )

        self.log(
            f"{self.__class__.__name__} initialized. Device={self.device_id}, "
            f"Endpoint={self.zwave_endpoint}, CC={self.zwave_command_class}, "
            f"Keys(close/partial)={self.property_key_close}/{self.property_key_partial}, "
            f"Positions(close/partial)={self.close_position}/{self.partial_position}, "
            f"Closed threshold={self.closed_threshold}%"
        )

    def _register_hold_listener(self, handler, property_key, description):
        self.listen_event(
            handler,
            "zwave_js_value_notification",
            device_id=self.device_id,
            command_class=self.zwave_command_class,
            endpoint=self.zwave_endpoint,
            property_key=property_key,
            value=self.event_value_held,
        )
        linked_hint = ""
        if self.bathroom_blind_entity:
            if property_key == self.property_key_close:
                linked_hint = f"; linked: {self.bathroom_blind_entity} -> 100% when bedroom is already >= {self.closed_threshold}% or closing from ~{self.partial_position}%"
            elif property_key == self.property_key_partial:
                linked_hint = f"; linked: {self.bathroom_blind_entity} -> {self.bathroom_partial_position}% if any blind is >= {self.closed_threshold}%"
        self.log(
            f"Listening for Z-Wave JS hold: scene={property_key} value={self.event_value_held} cc={self.zwave_command_class} ep={self.zwave_endpoint} -> {description} (target: {self.target_cover}){linked_hint}"
        )

    def _verify_entities_on_startup(self):
        """Verify that cover entities are available and have required attributes."""
        try:
            state = self.get_state(self.target_cover, attribute="current_position")
            if state is None:
                self.log(f"Warning: {self.target_cover} does not have 'current_position' attribute. Position-based logic may not work correctly.", level="WARNING")
            else:
                self.log(f"Verified {self.target_cover} is available (current_position={state})", level="DEBUG")
        except Exception as e:
            self.error(f"Failed to verify {self.target_cover}: {e}")

        if self.bathroom_blind_entity:
            try:
                state = self.get_state(self.bathroom_blind_entity, attribute="current_position")
                if state is None:
                    self.log(f"Warning: {self.bathroom_blind_entity} does not have 'current_position' attribute. Position-based logic may not work correctly.", level="WARNING")
                else:
                    self.log(f"Verified {self.bathroom_blind_entity} is available (current_position={state})", level="DEBUG")
            except Exception as e:
                self.error(f"Failed to verify {self.bathroom_blind_entity}: {e}")

    def _handle_close_hold(self, event_name, data, kwargs):
        """Handle hold on property_key_close -> set cover to close_position."""
        # Debug logging for event reception
        self.log(
            f"DEBUG: Received close-hold event - event_name={event_name}, data={data}, kwargs={kwargs}",
            level="DEBUG",
        )

        # Cooldown: prevent repeated service calls during continuous hold
        now_ts = self.datetime().timestamp()
        if self._last_close_hold_ts is not None and (now_ts - self._last_close_hold_ts) < self.hold_cooldown_seconds:
            self.log(
                f"Ignoring close-hold (cooldown {self.hold_cooldown_seconds}s)",
                level="DEBUG",
            )
            return
        self._last_close_hold_ts = now_ts

        # Use run_in to ensure sequential state checking and avoid race conditions
        self.run_in(self._execute_close_hold, self.state_check_delay)

    def _execute_close_hold(self, kwargs):
        """Execute close-hold action after delay for state consistency."""
        # Check current bedroom blind position before acting, to enable linked bathroom action
        bedroom_pos = None
        try:
            pos_str = self.get_state(self.target_cover, attribute="current_position")
            bedroom_pos = int(pos_str) if pos_str is not None else None
        except Exception as e:
            self.error(f"Failed to get bedroom blind position (entity={self.target_cover}): {e}")
            bedroom_pos = None

        # Only adjust the bathroom blind when sending a close (up) command AND
        # the bedroom is NOT at the configured partial position (~38)
        is_bedroom_partial = (
            bedroom_pos is not None and abs(bedroom_pos - self.partial_position) <= self.partial_match_tolerance
        )
        if self.bathroom_blind_entity and bedroom_pos is not None and not is_bedroom_partial:
            try:
                self.call_service(
                    "cover/set_cover_position",
                    entity_id=self.bathroom_blind_entity,
                    position=100,
                )
                self.log(
                    f"Close-hold: Bedroom at {bedroom_pos}% (not ~{self.partial_position}%). Set {self.bathroom_blind_entity} to 100% (LINKED_CLOSE)",
                    level="INFO",
                )
                # Verify bathroom blind position after service call
                self.run_in(
                    self._verify_position,
                    self.verification_delay,
                    entity_id=self.bathroom_blind_entity,
                    expected_position=100,
                    reason="LINKED_CLOSE",
                )
            except Exception as e:
                self.error(
                    f"Failed setting linked bathroom blind to 100% (bedroom_pos={bedroom_pos}%, entity={self.bathroom_blind_entity}): {e}"
                )

        # Always set bedroom blind to target close position (idempotent if already at target)
        self._set_cover_position(self.close_position, reason="CLOSE_HOLD")

    def _handle_partial_hold(self, event_name, data, kwargs):
        """Handle hold on property_key_partial (people button).

        New behavior (per UX requirement):
        - If BOTH blinds are closed -> open BOTH to their default positions
          (bedroom -> partial_position, bathroom -> bathroom_partial_position)
        - If ONE blind is closed -> open ONLY that blind to its default position
        - If NO blind is closed -> do nothing
        """
        # Debug logging for event reception
        self.log(
            f"DEBUG: Received partial-hold event - event_name={event_name}, data={data}, kwargs={kwargs}",
            level="DEBUG",
        )

        # Cooldown: prevent repeated service calls during continuous hold
        now_ts = self.datetime().timestamp()
        if self._last_partial_hold_ts is not None and (now_ts - self._last_partial_hold_ts) < self.hold_cooldown_seconds:
            self.log(
                f"Ignoring partial-hold (cooldown {self.hold_cooldown_seconds}s)",
                level="DEBUG",
            )
            return
        self._last_partial_hold_ts = now_ts

        # Use run_in to ensure sequential state checking and avoid race conditions
        self.run_in(self._execute_partial_hold, self.state_check_delay)

    def _execute_partial_hold(self, kwargs):
        """Execute partial-hold action after delay for state consistency."""
        bed_pos = None
        bath_pos = None
        try:
            bed_pos_str = self.get_state(self.target_cover, attribute="current_position")
            bed_pos = int(bed_pos_str) if bed_pos_str is not None else None
        except Exception as e:
            self.error(f"Failed to get bedroom blind position (entity={self.target_cover}): {e}")
            bed_pos = None

        if self.bathroom_blind_entity:
            try:
                bath_pos_str = self.get_state(self.bathroom_blind_entity, attribute="current_position")
                bath_pos = int(bath_pos_str) if bath_pos_str is not None else None
            except Exception as e:
                self.error(f"Failed to get bathroom blind position (entity={self.bathroom_blind_entity}): {e}")
                bath_pos = None

        # Evaluate closed states via cover_util (shared threshold; handles the 99%
        # low-battery park). bed_pos/bath_pos are still read above for logging.
        bedroom_closed = cover_util.is_closed(
            self, self.target_cover, threshold=self.closed_threshold
        )
        bathroom_closed = bool(self.bathroom_blind_entity) and cover_util.is_closed(
            self, self.bathroom_blind_entity, threshold=self.closed_threshold
        )

        # Case 1: Both closed -> open both to default positions
        if bedroom_closed and bathroom_closed:
            self._set_cover_position(self.partial_position, reason="PARTIAL_HOLD_OPEN_BOTH_BEDROOM")
            if self.bathroom_blind_entity:
                try:
                    self.call_service(
                        "cover/set_cover_position",
                        entity_id=self.bathroom_blind_entity,
                        position=self.bathroom_partial_position,
                    )
                    self.log(
                        f"PARTIAL_HOLD_OPEN_BOTH_BATHROOM: Set {self.bathroom_blind_entity} to {self.bathroom_partial_position}% (bedroom={bed_pos}%, bathroom={bath_pos}%)",
                        level="INFO",
                    )
                    # Verify bathroom blind position after service call
                    self.run_in(
                        self._verify_position,
                        self.verification_delay,
                        entity_id=self.bathroom_blind_entity,
                        expected_position=self.bathroom_partial_position,
                        reason="PARTIAL_HOLD_OPEN_BOTH_BATHROOM",
                    )
                except Exception as e:
                    self.error(
                        f"Failed setting linked bathroom blind to {self.bathroom_partial_position}% (bedroom={bed_pos}%, bathroom={bath_pos}%, entity={self.bathroom_blind_entity}): {e}"
                    )
            return

        # Case 2: Only bedroom closed -> open bedroom only
        if bedroom_closed and not bathroom_closed:
            self._set_cover_position(self.partial_position, reason="PARTIAL_HOLD_OPEN_BEDROOM_ONLY")
            return

        # Case 3: Only bathroom closed -> open bathroom only
        if bathroom_closed and not bedroom_closed and self.bathroom_blind_entity:
            try:
                self.call_service(
                    "cover/set_cover_position",
                    entity_id=self.bathroom_blind_entity,
                    position=self.bathroom_partial_position,
                )
                self.log(
                    f"PARTIAL_HOLD_OPEN_BATHROOM_ONLY: Set {self.bathroom_blind_entity} to {self.bathroom_partial_position}% (bedroom={bed_pos}%, bathroom={bath_pos}%)",
                    level="INFO",
                )
                # Verify bathroom blind position after service call
                self.run_in(
                    self._verify_position,
                    self.verification_delay,
                    entity_id=self.bathroom_blind_entity,
                    expected_position=self.bathroom_partial_position,
                    reason="PARTIAL_HOLD_OPEN_BATHROOM_ONLY",
                )
            except Exception as e:
                self.error(
                    f"Failed setting linked bathroom blind to {self.bathroom_partial_position}% (bedroom={bed_pos}%, bathroom={bath_pos}%, entity={self.bathroom_blind_entity}): {e}"
                )
            return

        # Case 4: Neither closed -> do nothing (explicit log for traceability)
        self.log(
            f"PARTIAL_HOLD: Neither blind is closed -> no action taken (bedroom={bed_pos}%, bathroom={bath_pos}%, threshold={self.closed_threshold}%)",
            level="INFO",
        )

    def _set_cover_position(self, position, reason=""):
        """Set cover position and verify after delay."""
        try:
            # Get current position for context in logs
            current_pos = None
            try:
                pos_str = self.get_state(self.target_cover, attribute="current_position")
                current_pos = int(pos_str) if pos_str is not None else None
            except Exception:
                pass

            self.call_service(
                "cover/set_cover_position",
                entity_id=self.target_cover,
                position=position,
            )
            pos_context = f" (from {current_pos}%)" if current_pos is not None else ""
            self.log(
                f"Set {self.target_cover} to position {position}%{pos_context} ({reason})",
                level="INFO",
            )
            # Verify position after service call
            self.run_in(
                self._verify_position,
                self.verification_delay,
                entity_id=self.target_cover,
                expected_position=position,
                reason=reason,
            )
        except Exception as e:
            current_pos = None
            try:
                pos_str = self.get_state(self.target_cover, attribute="current_position")
                current_pos = int(pos_str) if pos_str is not None else None
            except Exception:
                pass
            pos_context = f" (current={current_pos}%)" if current_pos is not None else ""
            self.error(
                f"Failed setting {self.target_cover} to {position}%{pos_context} ({reason}): {e}"
            )

    def _verify_position(self, kwargs):
        """Verify that cover reached expected position after service call."""
        entity_id = kwargs.get("entity_id")
        expected_position = kwargs.get("expected_position")
        reason = kwargs.get("reason", "UNKNOWN")

        if not entity_id or expected_position is None:
            self.log(f"Position verification skipped: missing entity_id or expected_position", level="WARNING")
            return

        try:
            actual_pos_str = self.get_state(entity_id, attribute="current_position")
            if actual_pos_str is None:
                self.log(
                    f"Position verification: {entity_id} does not have current_position attribute (reason={reason})",
                    level="WARNING"
                )
                return

            actual_pos = int(actual_pos_str)
            # Allow tolerance for position verification (especially for low battery cases)
            tolerance = 5  # Allow 5% tolerance
            if abs(actual_pos - expected_position) <= tolerance:
                self.log(
                    f"Position verified: {entity_id} at {actual_pos}% (expected {expected_position}%, reason={reason})",
                    level="DEBUG",
                )
            else:
                self.log(
                    f"Position mismatch: {entity_id} at {actual_pos}% (expected {expected_position}%, diff={abs(actual_pos - expected_position)}%, reason={reason}). "
                    f"This may indicate low battery or motor issue.",
                    level="WARNING"
                )
        except Exception as e:
            self.error(
                f"Failed to verify position for {entity_id} (expected={expected_position}%, reason={reason}): {e}"
            )


