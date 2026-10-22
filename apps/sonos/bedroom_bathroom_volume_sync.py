# /conf/apps/sonos/bedroom_bathroom_volume_sync.py
import appdaemon.plugins.hass.hassapi as hass  # type: ignore

class BedroomBathroomVolumeSync(hass.Hass):
    """
    While the bathroom door has been continuously open for door_open_debounce_sec, the bedroom
    Sonos speaker's volume tracks the bathroom speaker's volume - one-directional (bathroom
    leads, bedroom follows, never the reverse), only while BOTH speakers are actively playing.
    An open bathroom door acoustically connects the two rooms, so the bedroom should match the
    (usually louder, e.g. shower running) bathroom rather than sounding relatively quiet.

    Door closing deactivates tracking immediately - no forced restore, bedroom stays at whatever
    volume it last matched. Reactivates only after another full door_open_debounce_sec of
    continuous open.

    While the wake-up plays out and someone is still in the bed, the bedroom copies the bathroom
    only up to the bedroom's standard volume: WakeupRoutine.wake_volume_cap says so, and says
    nothing once the bed reads empty, so the volume follows a person who has walked to the
    bathroom. The window and the bed are the wake routine's to judge - this app only asks. When
    the wake routine cannot be asked, the sync behaves as if there were no wake-up.
    """

    def initialize(self):
        self.bathroom_door_sensor = self.args.get("bathroom_door_sensor", "binary_sensor.bathroom_door_contact")
        self.bathroom_speaker = self.args.get("bathroom_speaker", "media_player.bathroom")
        self.bedroom_speaker = self.args.get("bedroom_speaker", "media_player.bedroom")
        self.door_open_debounce_sec = int(self.args.get("door_open_debounce_sec", 120))
        self.wake_app = self.args.get("wake_app", "WakeupBedroom")

        self._sync_active = False
        self._sync_arm_timer = None

        self.listen_state(self._on_door_change, self.bathroom_door_sensor)
        self.listen_state(self._on_bathroom_volume_change, self.bathroom_speaker, attribute="volume_level")
        self.listen_state(self._on_speaker_state_change, self.bathroom_speaker)
        self.listen_state(self._on_speaker_state_change, self.bedroom_speaker)

        # Restart-safe: pick up an already-open door instead of waiting for the next edge.
        if self.get_state(self.bathroom_door_sensor) == "on":
            self._arm_sync_timer()

        self.log("Scenario: BedroomBathroomVolumeSync_loaded", level="INFO")

    def _on_door_change(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self._arm_sync_timer()
        elif new == "off":
            self._cancel_sync_timer()
            if self._sync_active:
                self._sync_active = False
                self.log("Scenario: volume_sync_deactivated -> bathroom door closed", level="INFO")
        # unavailable/unknown: no-op (hold current state, matches the door-contact convention
        # used elsewhere in this codebase - e.g. bedroom_lights._is_bathroom_door_open)

    def _arm_sync_timer(self):
        if self._sync_active or self._sync_arm_timer is not None:
            return
        self._sync_arm_timer = self.run_in(self._sync_timer_fire, self.door_open_debounce_sec)

    def _cancel_sync_timer(self):
        if self._sync_arm_timer is not None:
            try:
                self.cancel_timer(self._sync_arm_timer)
            except Exception:
                pass
            self._sync_arm_timer = None

    def _sync_timer_fire(self, kwargs):
        self._sync_arm_timer = None
        if self.get_state(self.bathroom_door_sensor) != "on":
            return  # door closed before the debounce elapsed (normally already cancelled; guard anyway)
        self._sync_active = True
        self.log(
            f"Scenario: volume_sync_activated -> bathroom door open {self.door_open_debounce_sec}s",
            level="INFO",
        )
        self._maybe_sync("activated")

    def _on_bathroom_volume_change(self, entity, attribute, old, new, kwargs):
        self._maybe_sync("bathroom_volume_change")

    def _on_speaker_state_change(self, entity, attribute, old, new, kwargs):
        self._maybe_sync("speaker_state_change")

    def _wake_cap(self):
        try:
            cap = self.get_app(self.wake_app).wake_volume_cap(self.bedroom_speaker)
            return None if cap is None else float(cap)
        except Exception:
            return None

    def _maybe_sync(self, reason):
        if not self._sync_active:
            return
        if self.get_state(self.bathroom_speaker) != "playing" or self.get_state(self.bedroom_speaker) != "playing":
            return
        try:
            bath_vol = float(self.get_state(self.bathroom_speaker, attribute="volume_level"))
        except (TypeError, ValueError):
            return
        try:
            bed_vol = float(self.get_state(self.bedroom_speaker, attribute="volume_level"))
        except (TypeError, ValueError):
            bed_vol = None
        cap = self._wake_cap()
        target = bath_vol if cap is None else min(bath_vol, cap)
        if bed_vol is not None and abs(bed_vol - target) < 0.005:
            return
        capped = "" if target == bath_vol else f", capped at the wake standard {cap:.2f}"
        self.log(
            f"Scenario: volume_sync_apply -> {reason}, bedroom {bed_vol} -> {target:.2f} (matching bathroom{capped})",
            level="INFO",
        )
        self.call_service("media_player/volume_set", entity_id=self.bedroom_speaker, volume_level=target)
