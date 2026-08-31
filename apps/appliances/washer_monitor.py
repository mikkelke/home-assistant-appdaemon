"""
WasherMonitor - Tracks washer state with power monitoring and program detection.

Appliance: Miele WEA 035 WCS Active (Operating instructions M.-Nr. 11 592 880).
Consumption data / programme table: manual page 62 (see washer.yaml for the table).

Features:
    - AddLoad: "ADD" displayed when door can be opened mid-cycle to add laundry
    - Express: ~30 min, ~0.3 kWh
    - Normal: ~60-90 min, ~0.51 kWh
    - Sanitize: up to 180 min

States:
    - Off: Washer is idle, clean, or emptied
    - Running: Wash cycle is in progress
    - Paused: Door opened during Running - AddLoad only before first heating (manual p.22); else power-based pause or done
    - Unemptied: Cycle completed (power dropped), door still closed - waiting for user
    - Emptied: Door opened after cycle complete - user is emptying (door still open)

State Transitions:
    - Power goes high while Off -> Running
    - Power drops while Running (door closed) -> Unemptied (reminder to empty)
    - Door opens while Running + within addload window + no heating yet -> Paused (AddLoad)
    - Door opens while Running + past addload window -> Unemptied+Emptied+Off (EU front-load: wash done before door; no power gate)
    - Door opens while Running + within window but heating started -> Unemptied+Emptied if power looks like end, else Paused
    - Door closes from Paused + power HIGH -> Running (cycle resumes)
    - Door closes from Paused + power LOW + valid cycle -> Unemptied
    - Door closes from Paused + power LOW + invalid cycle -> Off
    - Door opens while Unemptied -> Emptied (user is emptying)
    - Door closes while Emptied -> Off (emptying complete)

Duration, progress, announcement:
    - Programme length is predicted from sensors (power, energy, runtime) and known info:
      user-selected programme + temperature when set, else classified programme; learned
      durations from confirmed cycles refine the estimate over time.
    - Finish: We detect cycle end from energy/power (BEFORE the door opens). When run is in the
      last hour of expected duration we use a shorter stable window (finish_stable_minutes_near_end)
      so we transition to Unemptied when the machine actually stops (~10:52), not when the user
      opens the door (11:05).
    - Start: We only declare Running on sustained high power; start_time is clamped to
      last_off_at and to last_door_closed_at only when that timestamp is trusted (set on a real
      door close, not loaded from stale entity data after HA restart). Attribute
      last_door_closed_trusted is persisted on the state entity for this.
    - Running state exposes: programme_duration_min, elapsed_minutes, progress_pct (0-100),
      estimated_remaining_min, estimated_end_time so dashboards can show how far the cycle is.
    - Announcement ("washer ready to empty"): by default when we detect cycle end (Unemptied).
      Optional door_lock_entity: announce when the door lock goes to "unlocked" instead.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import collections
import time
import os
import uuid
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Sibling modules in this same app directory (AppDaemon puts app dirs on sys.path;
# same flat-import style as apps/climate/smart_cooling.py's `import climate_model as cm`).
# These hold the pure parts - tables, signal maths, classification, feedback shaping -
# while every stateful, AppDaemon-coupled method stays on WasherMonitor below.
import washer_classify as wcls
import washer_feedback as wfb
import washer_history as whist
import washer_power as wpow
import washer_profiles as wp
import cycle_store as cystore

# Shared save/restore-snapshot/staleness-check plumbing for the on-disk store above - see
# cycle_persistence.py's module docstring for the split between what it owns and what stays
# here (boot resolution policy, detection/guard/classification logic). Imported defensively,
# same shape as dryer_monitor.py's cycle_store import, so it resolves under AppDaemon's loader.
try:
    from cycle_persistence import CyclePersistenceMixin
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cycle_persistence import CyclePersistenceMixin

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore


# Kept as a module-level name so the many `_parse_utc(...)` call sites below,
# and anything importing it from this module, stay unchanged.
_parse_utc = whist.parse_utc

# Provenance ranking for self.start_time: lower = more trustworthy. entity_last_changed
# (rank 6) is the weakest signal - after an HA restart erases and recreates the AppDaemon
# state entity, its last_changed means "we just recreated this", not a real cycle-2 start.
# Ranking lets that heuristic defer to anything a stronger source already claimed instead of
# overwriting it every few minutes. See _start_time_rank().
_START_TIME_SOURCE_RANK = {
    "door_close_trusted": 1,
    "live": 2,
    "durable_store": 3,  # restored from cycle_store.py - see initialize()'s boot resolution
    "state_history": 4,
    "power_history": 5,
    "entity_last_changed": 6,
}


class WasherMonitor(CyclePersistenceMixin, hass.Hass):
    # Programme profiles loaded from washer_programmes.yaml at startup.
    # Programme and temperature are independent dimensions.  For "bomuld",
    # the profile depends on the selected temperature (by_temperature dict).
    # UI matrix: allowed_temperatures, allowed_spin_speeds, default_temperature, default_spin, available_options (canonical values only).
    # Tables live in washer_profiles.py; aliased here so existing self._NAME call
    # sites and any external reader keep working unchanged.
    _PROGRAMME_DISPLAY_ORDER = wp.PROGRAMME_DISPLAY_ORDER
    _CANONICAL_SPIN = wp.CANONICAL_SPIN
    _DEFAULT_PROFILES = wp.DEFAULT_PROFILES
    PROGRAMME_PROFILES = dict(wp.DEFAULT_PROFILES)


    def _get_profile(self, programme: str, temperature=None):
        """Return the flat profile dict for a programme + optional temperature.
        See washer_profiles.get_profile; bound here to the live PROGRAMME_PROFILES."""
        return wp.get_profile(self.PROGRAMME_PROFILES, programme, temperature)

    def _programme_has_temperature(self, programme: str) -> bool:
        """Return True if this programme has temperature-dependent profiles (e.g. bomuld).
        Only then do we persist/learn temperature; otherwise learn_key is just programme."""
        return wp.programme_has_temperature(self.PROGRAMME_PROFILES, programme)

    def _load_programme_profiles(self):
        """Load programme profiles from washer_programmes.yaml if present. Merge with defaults
        so we never lose e.g. 'unknown' or any default keys; YAML overrides/extends only.
        """
        prog_file = self.args.get("programmes_file")
        if not prog_file:
            prog_file = os.path.join(os.path.dirname(__file__), "washer_programmes.yaml")
        try:
            with open(prog_file, "r") as f:
                data = yaml.safe_load(f) or {}
            profiles = data.get("programmes", {})
            order = data.get("programme_display_order")
            self._programme_display_order = order if isinstance(order, list) and order else list(self._PROGRAMME_DISPLAY_ORDER)
            if profiles:
                merged = wp.merge_profiles(profiles)
                WasherMonitor.PROGRAMME_PROFILES = merged
                # Build label -> key from profiles and stable display order (so new YAML programmes appear)
                WasherMonitor._LABEL_TO_KEY = wp.build_label_to_key(merged, self._programme_display_order)
                self.log(f"Loaded {len(profiles)} programme profiles from {prog_file}", level="INFO")
            else:
                self.log(f"No 'programmes' key in {prog_file} - using defaults", level="WARNING")
                self._programme_display_order = list(self._PROGRAMME_DISPLAY_ORDER)
        except FileNotFoundError:
            self.log(f"Programme file {prog_file} not found - using defaults", level="WARNING")
            self._programme_display_order = list(self._PROGRAMME_DISPLAY_ORDER)
        except Exception as exc:
            self.log(f"Failed to load {prog_file}: {exc} - using defaults", level="ERROR")
            self._programme_display_order = list(self._PROGRAMME_DISPLAY_ORDER)

    def _safe_cancel_timer(self, handle):
        """Cancel a timer only if still running (avoids invalid-handle warnings)."""
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
                return True
        except Exception:
            pass
        return False

    def _now_utc(self):
        """Return current time as timezone-aware UTC. Uses epoch to avoid any system timezone mix-up
        (e.g. 08:57 local must become 07:57 UTC for Copenhagen, not 08:57Z)."""
        return datetime.fromtimestamp(time.time(), timezone.utc)

    def _local_tz(self):
        """Return the configured local timezone (e.g. Europe/Copenhagen) for storage and display."""
        return getattr(self, "_local_tz_obj", None) or timezone.utc

    def _format_local(self, dt):
        """Format a datetime for storage/display in the configured timezone (ISO with offset)."""
        if dt is None:
            return ""
        tz = self._local_tz()
        return dt.astimezone(tz).isoformat(timespec="seconds")

    def _format_utc(self, dt):
        """Format a datetime as UTC ISO with Z suffix. For cycle_start_time so frontend can parse as UTC and use toLocaleTimeString().
        dt must be timezone-aware (from _now_utc() or _parse_utc()); we never pass local time with a Z suffix."""
        if dt is None:
            return ""
        utc = dt.astimezone(timezone.utc)
        return utc.isoformat(timespec="seconds").replace("+00:00", "Z")

    def _strftime_local(self, dt, fmt="%H:%M"):
        """Format a datetime in local time for logs / speaking in your timezone."""
        if dt is None:
            return ""
        return dt.astimezone(self._local_tz()).strftime(fmt)

    def _start_time_rank(self):
        """Trust rank of self.start_time's current provenance (lower = more trustworthy).
        Unclaimed provenance ranks weakest (99) so existing behaviour is unchanged wherever
        nothing has claimed it yet. getattr, not a bare attribute read: the callers sit inside
        `except (TypeError, ValueError, AttributeError)` blocks, so on an instance that never
        ran initialize() an AttributeError here would not surface - it would silently skip the
        whole surrounding correction, which is exactly how it slipped past a green test run."""
        return _START_TIME_SOURCE_RANK.get(getattr(self, "_start_time_source", None), 99)

    # Strips the degree sign so log output can't hit an encoding error.
    _log_safe = staticmethod(wcls.log_safe)

    def _attr_bool_true(self, val) -> bool:
        """HA/AppDaemon entity attributes may be bool or string."""
        return val is True or val == "true" or val == "True"

    _APPLIANCE_UI_STATES = frozenset({"Off", "Running", "Paused", "Unemptied", "Emptied"})

    def _sync_ui_select(self, state_str):
        """Mirror state string to input_select helper (Lovelace survives HA restart)."""
        sel = getattr(self, "ui_state_select", None)
        if not sel or state_str not in self._APPLIANCE_UI_STATES:
            return
        try:
            self.call_service("input_select/select_option", entity_id=sel, option=state_str)
        except Exception as e:
            self.log(f"ui_state_select sync failed ({sel!r} -> {state_str}): {e}", level="DEBUG")

    # _set_state_entity / _save_cycle_state / _build_cycle_store_payload are inherited from
    # CyclePersistenceMixin - see cycle_persistence.py for the shared mechanism (throttle,
    # Off-clears only once self.state itself agrees, CycleStore.save() gating). Only what is
    # genuinely washer-specific is supplied below: the on-disk shape predates that refactor and
    # differs from the dryer's/dishwasher's in four small, deliberate respects - "" instead of
    # null for an unset optional field, and state_since/cycle_id living under their own
    # underscore-prefixed attribute names rather than state_since/cycle_id directly.
    _cycle_store_empty_value = ""
    # Unlike the dryer/dishwasher, washer can reach a store write with a PUBLISHED state that
    # disagrees with self.state (_on_confirm_changed republishes a value read back from HA, not
    # self.state - see _save_cycle_state's own docstring in the mixin) - an Off publish must
    # only clear the durable store when self.state is genuinely Off too.
    _cycle_store_off_requires_live_state = True

    def _cycle_store_state_since(self):
        return self._store_state_since

    def _cycle_store_cycle_id(self):
        return self._cycle_id

    def _cycle_store_before_save(self, state, state_changed, now):
        """state_since is stamped the moment the persisted state STRING changes (never on a
        change to any of the other trigger fields below), BEFORE the payload is built - so the
        very save that records a transition also records the correct entry time for it, rather
        than lagging one save behind (a save right after a transition would otherwise persist
        the PREVIOUS state's state_since alongside the NEW state)."""
        if state_changed:
            self._store_state_since = now

    def _cycle_store_trigger_fields(self):
        """Beyond the state string (always checked), a change to any of these forces an
        immediate write rather than waiting out store_min_write_interval_s - _check_energy_finish
        republishes "Running" every energy_check_interval_s (30s default), and every other
        tick-driven publish is just as frequent, so a disk write on every one of those would be
        pure waste; these are the fields whose staleness would otherwise matter within that
        window."""
        return (
            lambda: self.start_time,
            lambda: bool(self.notification_sent),
            lambda: self.detected_programme,
            lambda: self.detected_temperature,
            lambda: self.expected_dur_at_start,
            lambda: self._guard_bar_class,
            lambda: bool(self.programme_confirmed_by_user),
            lambda: self.last_door_closed_at,
        )

    def _cycle_store_last_off_at(self):
        # A live read (not a self.* attribute like every other field below) - wrapped in its
        # own try/except so a failure here can never take the whole payload build down with it.
        try:
            return self.get_state(self.state_entity, attribute="last_off_at") or ""
        except Exception:
            return ""

    def _cycle_store_field_map(self):
        """Non-envelope fields for the on-disk payload - the common envelope (state,
        state_since, cycle_id, entity_recreated_at) is handled once, in the mixin. Deliberately
        excludes last_state_change (see cycle_persistence.py's _build_cycle_store_payload for
        why), timer handles, power_readings, energy_buffer
        (_restore_energy_state_from_history re-derives it), the vibration counters
        (telemetry-only by contract), and _last_infer_start_attempt (a throttle). The
        delayed-start machine (_delay_waiting / _delay_plateau_start) is also left out
        deliberately - see the comment on delayed_start_trimmed below."""
        return {
            "start_time": lambda: cystore.format_utc(self.start_time) if self.start_time else "",
            "last_door_closed_at": (
                lambda: cystore.format_utc(self.last_door_closed_at) if self.last_door_closed_at else ""
            ),
            "last_door_closed_trusted": lambda: bool(self.last_door_closed_trusted),
            "last_off_at": self._cycle_store_last_off_at,
            "energy_at_start": lambda: self.energy_start,
            "started_by": lambda: (self._cycle_actor or {}).get("person") or "",
            "started_by_method": lambda: (self._cycle_actor or {}).get("method") or "unknown",
            "session_cost_kr": lambda: self._session_cost_kr,
            "detected_programme": lambda: self.detected_programme,
            "detected_temperature": lambda: self.detected_temperature,
            "observed_heating": lambda: bool(self.observed_heating),
            "heating_phase_count": lambda: self.heating_phase_count,
            "max_power_seen": lambda: self.max_power_seen,
            "expected_dur_at_start": lambda: self.expected_dur_at_start,
            "expected_dur_key": lambda: self._guard_bar_key_str(),
            "programme_confirmed_by_user": lambda: bool(self.programme_confirmed_by_user),
            "programme_confirmed_by": lambda: self.confirmed_by_username or "",
            # Deliberately deferred: _delay_waiting / _delay_plateau_start interact with
            # _slide_start_for_delayed_start in ways needing separate study - only the
            # already-applied outcome (delayed_start_trimmed) is persisted.
            "delayed_start_trimmed": lambda: bool(self._delayed_start_trimmed),
            "last_high_energy_at": (
                lambda: cystore.format_utc(self.last_high_energy_at) if self.last_high_energy_at else ""
            ),
            "notification_sent": lambda: bool(self.notification_sent),
            "finish_confirmed": lambda: bool(self.finish_confirmed),
            "in_finishing_tail": lambda: bool(self.in_finishing_tail),
            "in_finishing_tail_entered_at": (
                lambda: cystore.format_utc(self.in_finishing_tail_entered_at)
                if self.in_finishing_tail_entered_at else ""
            ),
            "last_tail_pulse_at": (
                lambda: cystore.format_utc(self.last_tail_pulse_at) if self.last_tail_pulse_at else ""
            ),
            "tail_pattern_locked": lambda: bool(self.tail_pattern_locked),
            "tail_pattern_cycle_seconds": lambda: self.tail_pattern_cycle_seconds,
            "tail_pattern_last_pulse_at": (
                lambda: cystore.format_utc(self.tail_pattern_last_pulse_at) if self.tail_pattern_last_pulse_at else ""
            ),
            "tail_pattern_locked_at": (
                lambda: cystore.format_utc(self.tail_pattern_locked_at) if self.tail_pattern_locked_at else ""
            ),
            "door_opened_during_cycle": lambda: bool(self.door_opened_during_cycle),
        }

    def _cycle_store_payload(self, state_str) -> dict:
        """Historical name, kept as a thin alias so existing references (including this test
        suite's own comments) keep resolving - see _build_cycle_store_payload (mixin)."""
        return self._build_cycle_store_payload(state_str)

    def _push_corrected_start_time_to_entity(self):
        """Push current self.start_time to the state entity (cycle_start_time, cycle_start_time_local, started_at_display). Preserves other attributes."""
        try:
            full = self.get_state(self.state_entity, attribute="all")
            attrs = dict((full or {}).get("attributes") or {})
            attrs["cycle_start_time"] = self._format_utc(self.start_time)
            attrs["cycle_start_time_local"] = self._format_local(self.start_time)
            attrs["started_at_display"] = self.start_time.astimezone(self._local_tz()).strftime("%H:%M")
            attrs["last_door_closed_trusted"] = bool(self.last_door_closed_trusted)
            # last_door_closed_trusted silently drops from published attributes whenever it's
            # False (no trusted door-close yet, e.g. after a restart) -- AppDaemon 4.5.13
            # set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity(state="Running", attributes=attrs, replace=True)
        except Exception as e:
            self.log(f"Could not push corrected cycle_start_time: {e}", level="WARNING")
            # last_door_closed_trusted silently drops from published attributes whenever it's
            # False (no trusted door-close yet, e.g. after a restart) -- AppDaemon 4.5.13
            # set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity(
                state="Running",
                attributes={
                    "cycle_start_time": self._format_utc(self.start_time),
                    "cycle_start_time_local": self._format_local(self.start_time),
                    "started_at_display": self.start_time.astimezone(self._local_tz()).strftime("%H:%M"),
                    "last_door_closed_trusted": bool(self.last_door_closed_trusted),
                },
            )

    def initialize(self):
        self._load_programme_profiles()
        # ----- Configuration -----
        self.power_sensor = self.args["power_sensor"]
        self.energy_sensor = self.args["energy_sensor"]
        self.door_sensor = self.args["door_sensor"]
        self.state_entity = self.args["state_entity"]
        self.ui_state_select = self.args.get("ui_state_entity")
        if self.ui_state_select == "":
            self.ui_state_select = None
        if not self.ui_state_select and self.state_entity and str(self.state_entity).startswith("sensor."):
            self.ui_state_select = "input_select." + str(self.state_entity).split(".", 1)[1]
        self.door_sensor_inverted = bool(self.args.get("door_sensor_inverted", False))

        # Vibration telemetry (HOBEIAN Zigbee shock sensor on the side panel; see washer.yaml
        # for the sensor rationale). TELEMETRY ONLY: nothing here may feed a state transition,
        # classification, delayed-start, ETA, or push decision - this only collects pulse data
        # into the per-cycle feedback record so real thresholds can be picked later. Unset/empty
        # -> sensor.get() is None, no listener is registered, and every helper below is a no-op.
        self.vibration_sensor = self.args.get("vibration_sensor") or None
        self.vibration_pulse_count = 0    # ON edges while state == "Running" (current cycle)
        self.vibration_on_seconds = 0.0   # Summed on-time for pulses that started while Running
        self.first_vibration_at = None    # UTC; first Running-scoped pulse this cycle
        self.last_vibration_at = None     # UTC; last Running-scoped pulse this cycle
        self._vibration_on_started = None  # (UTC started_at, was_running) for an open ON edge, else None
        # ALL on-edges regardless of state - survives per-cycle resets (used for the post-save
        # unload window, which runs after the cycle has already ended).
        self._vibration_events = collections.deque(maxlen=500)
        self._unload_patch_timer = None   # Pending _patch_unload_vibration handle (see _schedule_vibration_unload_patch)

        self.start_w = float(self.args["start_w"])
        self.stop_w = float(self.args["stop_w"])
        self.run_for = int(self.args.get("run_for", 60))
        self.programs = self.args.get("programs", {})
        # Plug/state entity unavailable: tolerate short gaps (HA restart, ESPHome OTA flash) before
        # force-wiping a Running cycle - see dishwasher_monitor.py's power_unavailable_error_after_seconds
        # for the same guard (2026-07-17 log investigation: dishwasher absorbs these with zero false
        # transitions; washer used to force Off instantly and destroy cycle tracking/learning data).
        self.power_unavailable_off_after_seconds = int(self.args.get("power_unavailable_off_after_seconds", 180))

        # Delayed start (Miele delay timer): a brief selection-burst power spike (up to ~60W for
        # ~15 min while the user picks a programme) trips start detection, then the machine sits
        # at flat standby for hours until the wash actually begins. Without this, the whole wait
        # is counted as wash time (see washer.yaml DELAYED START block for full rationale).
        self.detect_delayed_start = bool(self.args.get("detect_delayed_start", True))
        self.delay_plateau_minutes = int(self.args.get("delay_plateau_minutes", 30))
        self.delay_energy_floor_kwh = float(self.args.get("delay_energy_floor_kwh", 0.05))
        self.delayed_start_show_waiting = bool(self.args.get("delayed_start_show_waiting", True))

        # Cycle validation thresholds
        self.min_cycle_minutes = int(self.args.get("min_cycle_minutes", 25))
        self.min_energy_kwh = float(self.args.get("min_energy_kwh", 0.2))
        self.completion_guard_fraction = float(self.args.get("completion_guard_fraction", 0.65))
        self.completion_guard_fraction_user_confirmed = float(self.args.get("completion_guard_fraction_user_confirmed", 0.60))
        self.pause_timeout_minutes = int(self.args.get("pause_timeout_minutes", 10))
        self.addload_window_minutes = int(self.args.get("addload_window_minutes", 5))  # AddLoad only at start
        self.pause_window_minutes = int(self.args.get("pause_window_minutes", 3))  # Paused only relevant in first 3 min
        self.restore_start_gap_minutes = int(self.args.get("restore_start_gap_minutes", 15))  # min continuous low power after restored start to treat as stale and re-infer from history
        # Restore anchors this close to the app's own init are the restart re-creating the
        # entity, not information (2026-08-12: entity last_changed 13:24 after an HA restart
        # dragged a real 11:41 cycle start to 13:24). See _restore_running_state.
        self.restore_wipe_anchor_grace_s = int(self.args.get("restore_wipe_anchor_grace_s", 300))

        # Local timezone for attributes and logs - use app timezone if set, else AppDaemon's time_zone (appdaemon.yaml)
        tz_name = self.args.get("timezone") or getattr(self.AD, "time_zone", None) or "Europe/Copenhagen"
        if ZoneInfo is not None:
            try:
                self._local_tz_obj = ZoneInfo(tz_name)
            except Exception:
                # Fallback when tzdata is missing (e.g. minimal Docker): use fixed offset for common zones
                if tz_name and "Copenhagen" in str(tz_name):
                    self._local_tz_obj = timezone(timedelta(hours=1))  # CET (winter); close enough for display
                else:
                    self._local_tz_obj = timezone.utc
                self.log(f"Timezone '{tz_name}' using fixed offset (install tzdata for full DST support)", level="DEBUG")
        else:
            self._local_tz_obj = timezone.utc
            self.log("zoneinfo not available, using UTC for display", level="DEBUG")

        self.log(f"Using timezone {tz_name} for attributes and logs", level="DEBUG")

        # Safety watchdogs - prevent stuck states
        self.max_running_hours = float(self.args.get("max_running_hours", 5))
        self.unemptied_timeout_hours = float(self.args.get("unemptied_timeout_hours", 12))
        # How long to stay in Emptied before auto-transitioning to Off (user may leave door open to dry).
        self.emptied_timeout_minutes = float(self.args.get("emptied_timeout_minutes", 30))

        # Power thresholds
        self.significant_w = float(self.args.get("significant_w", 30))
        self.no_recent_high_s = int(self.args.get("no_recent_high_s", 600))

        # Consecutive reading thresholds
        self.high_power_threshold = int(self.args.get("high_power_threshold", 3))
        self.low_power_threshold = int(self.args.get("low_power_threshold", 15))

        # Power readings buffer
        self.power_readings = []
        self.pattern_window = int(self.args.get("pattern_window", 12))

        # State tracking
        self.program_timer = None
        self.start_time = None
        self._start_time_source = None  # provenance of self.start_time - see _START_TIME_SOURCE_RANK / _start_time_rank()
        self._cycle_actor = None  # Who started the current cycle - see _attribute() / ActorAttribution app
        self._last_saved_record_ts = None  # ts of the last-saved feedback record - see _patch_cycle_record
        self.energy_start = None
        self.poll_timer = None
        self.history_poll_timer = None  # Periodic power-history check to catch missed heating
        self.last_state_change = None
        self.last_door_closed_at = None  # Last time door was closed (start time cannot be before this, except AddLoad)
        self.last_door_closed_trusted = False  # True only after a real door close (not stale entity / infer-only)
        self._entity_recreated_at = None  # UTC; stamped below when HA erased state_entity and we're about to recreate it
        self.door_close_fast_start_window_s = int(self.args.get("door_close_fast_start_window_s", 600))
        self.door_fast_start_armed_until = None  # UTC; armed only after close from Off/Emptied (not Paused)
        self.cooling_period = int(self.args.get("cooling_period", 300))
        
        # Energy-based finish detection (primary method)
        self.use_energy_detection = self.args.get("use_energy_detection", True)
        self.energy_stable_minutes = int(self.args.get("energy_stable_minutes", 15))  # Default 15 min
        self.energy_check_interval = int(self.args.get("energy_check_interval_s", 30))  # Check every 30 seconds
        # Energy stability detection: use implied watts instead of fixed kWh delta
        self.energy_stable_watts = float(self.args.get("energy_stable_watts", 30.0))  # Below this = true idle
        # Post-cycle slow spin: washer may keep motor at 30-80W after cycle; treat as "idle" so we don't wait for 0W.
        self.energy_active_watts = float(self.args.get("energy_active_watts", 100.0))   # Above this = main cycle (heating/spin)
        self.post_cycle_idle_watts = float(self.args.get("post_cycle_idle_watts", 80.0))  # Below this = idle or slow spin (can finish)
        # Post-cycle slow-spin pattern: regular low-amplitude ripple in power (distinct from flat idle).
        self.post_cycle_pattern_window_minutes = int(self.args.get("post_cycle_pattern_window_minutes", 10))
        self.post_cycle_pattern_minutes = int(self.args.get("post_cycle_pattern_minutes", 5))  # Required "low" time when pattern detected
        self.post_cycle_pattern_mean_low = float(self.args.get("post_cycle_pattern_mean_low", 10.0))
        self.post_cycle_pattern_mean_high = float(self.args.get("post_cycle_pattern_mean_high", 70.0))
        self.post_cycle_pattern_min_std = float(self.args.get("post_cycle_pattern_min_std", 8.0))  # Ripple has elevated std vs flat idle
        # When run time is near/past expected programme duration, use shorter stable window so we declare finish BEFORE door opens (real-life: cycle ends ~10:52, door opens 11:05).
        self.finish_stable_minutes_near_end = int(self.args.get("finish_stable_minutes_near_end", 5))
        # Anti-crease (post-end tail) detection: config-driven, raw power history as primary signal (independent from energy bookkeeping).
        self.anti_crease_window_minutes = float(self.args.get("anti_crease_window_minutes", 8))
        # Real anti-crease is very low power (idle + small tumbling bumps). Mid-cycle rinse can look similar
        # (mean ~50W, peaks 200W+) so we require low mean and optionally cap peak to avoid false positives.
        self.anti_crease_tail_max_mean_w = float(self.args.get("anti_crease_tail_max_mean_w", 40.0))
        self.anti_crease_tail_max_peak_w = self.args.get("anti_crease_tail_max_peak_w")  # None = disabled
        if self.anti_crease_tail_max_peak_w is not None:
            self.anti_crease_tail_max_peak_w = float(self.anti_crease_tail_max_peak_w)
        self.anti_crease_tail_min_std_w = float(self.args.get("anti_crease_tail_min_std_w", 6.0))
        self.anti_crease_max_duty_above_active = float(self.args.get("anti_crease_max_duty_above_active", 0.15))
        self.anti_crease_near_end_minutes = float(self.args.get("anti_crease_near_end_minutes", 25))
        self.anti_crease_min_runtime_minutes = float(self.args.get("anti_crease_min_runtime_minutes", 60))  # When programme unknown
        # Once run_min is STRICTLY past guard_dur (the same expected-duration source
        # _meets_finish_time_guards uses - not merely within anti_crease_near_end_minutes of it),
        # a confirmed anti-crease pattern IS the end signal: skip FinishingTail's tail-pulse wait
        # and announce immediately, mirroring the dryer's keep-fresh transition (~4 min detections).
        # Near-but-not-past-end still goes through the slower FinishingTail/_try_finish_via_standby path unchanged.
        self.anti_crease_announce_past_expected = bool(self.args.get("anti_crease_announce_past_expected", True))
        self.finish_debug_window_minutes = float(self.args.get("finish_debug_window_minutes", 25))  # When to emit finish/anti-crease debug logs
        # Stricter finish guards to stop false announcements when guard_dur is underestimated.
        self.finish_guard_fraction = float(self.args.get("finish_guard_fraction", 0.92))  # Require 92% of expected (was 85%)
        self.finish_min_run_minutes_warm = float(self.args.get("finish_min_run_minutes_warm", 100.0))  # Never finish warm cycle before this
        self.finish_min_run_minutes_cold = float(self.args.get("finish_min_run_minutes_cold", 50.0))   # Never finish cold/unknown before this
        # Power-pattern gate: only allow Unemptied when recent power looks like real end (anti-crease or off), not mid-cycle rinse.
        self.finish_power_gate_max_mean_w = float(self.args.get("finish_power_gate_max_mean_w", 45.0))
        self.finish_power_gate_max_peak_w = float(self.args.get("finish_power_gate_max_peak_w", 120.0))
        self.finish_power_gate_off_max_mean_w = float(self.args.get("finish_power_gate_off_max_mean_w", 12.0))
        self.finish_power_gate_off_max_peak_w = float(self.args.get("finish_power_gate_off_max_peak_w", 25.0))
        # Two-stage finish: FinishingTail (pulsing 15–50W) vs Finished. Announce when next tail pulse fails to arrive.
        self.standby_max_watts = float(self.args.get("standby_max_watts", 5.0))  # Power ≤ this = flat standby
        self.standby_no_pulse_above_watts = float(self.args.get("standby_no_pulse_above_watts", 10.0))
        self.standby_quiet_seconds = float(self.args.get("standby_quiet_seconds", 25.0))  # Legacy; tail_pulse_timeout_seconds is primary
        self.tail_pulse_threshold_watts = float(self.args.get("tail_pulse_threshold_watts", 10.0))  # Above this = tail pulse (update last_tail_pulse_at)
        # In FinishingTail only: nudges above this reset last_tail_pulse_at (default 80). Anti-crease 10–55W does not reset.
        self.finishing_tail_pulse_reset_watts = float(self.args.get("finishing_tail_pulse_reset_watts", 80.0))
        self.tail_pulse_timeout_seconds = float(self.args.get("tail_pulse_timeout_seconds", 55.0))  # No pulse for this long + low power = finished (data: 55s = 0 early triggers)
        self.finish_standby_max_watts = float(self.args.get("finish_standby_max_watts", 8.0))  # Current power must be ≤ this to announce
        # Extra reliability gate for standby transition: require a recent quiet window with no spin/tail spikes.
        self.tail_idle_confirm_seconds = float(self.args.get("tail_idle_confirm_seconds", 120.0))
        self.tail_idle_peak_max_watts = float(self.args.get("tail_idle_peak_max_watts", 18.0))
        # Tail cadence detector: lock to anti-crease/spin pulse rhythm and finish when the rhythm breaks.
        self.tail_pattern_pulse_threshold_watts = float(self.args.get("tail_pattern_pulse_threshold_watts", 20.0))
        self.tail_pattern_lock_window_minutes = float(self.args.get("tail_pattern_lock_window_minutes", 8.0))
        self.tail_pattern_lock_min_pulses = int(self.args.get("tail_pattern_lock_min_pulses", 6))
        self.tail_pattern_min_gap_seconds = float(self.args.get("tail_pattern_min_gap_seconds", 8.0))
        self.tail_pattern_max_gap_seconds = float(self.args.get("tail_pattern_max_gap_seconds", 120.0))
        self.tail_pattern_max_jitter_fraction = float(self.args.get("tail_pattern_max_jitter_fraction", 0.55))
        self.tail_pattern_break_missed_pulses = float(self.args.get("tail_pattern_break_missed_pulses", 2.2))
        self.tail_pattern_break_confirm_seconds = float(self.args.get("tail_pattern_break_confirm_seconds", 18.0))
        self.in_finishing_tail = False  # True when tail pattern or energy-stable detected; transition when tail-pulse timeout
        self.in_finishing_tail_entered_at = None
        self.last_tail_pulse_at = None  # Last time power went above _tail_pulse_reset_threshold_watts while in FinishingTail
        self.tail_pattern_locked = False
        self.tail_pattern_cycle_seconds = None
        self.tail_pattern_last_pulse_at = None
        self.tail_pattern_locked_at = None
        self.last_energy_value = None
        self.last_energy_time = None  # Track timestamp for watts calculation
        self.energy_stable_start_time = None
        self.last_high_energy_at = None  # Last time energy rate was above threshold
        self.energy_check_timer = None
        self.energy_buffer = []  # Rolling window of (datetime, kWh) for aliasing-resistant implied-watts

        # Settled per-cycle cost tracking (dedicated vars - not shared with the energy-buffer/
        # tail-detection vars above, which get re-seeded by unrelated paths).
        self._session_cost_kr = 0.0
        self._cost_prev_energy_kwh = None  # Previous tick's cumulative energy reading for cost delta

        # Finish confirmation flag
        self.finish_confirmed = False
        self._zero_power_since = None  # Standby backstop: when power first dropped to 0W
        # Pending end reason when transitioning from anti-crease path (so _transition_to_unemptied can store it in feedback).
        self._pending_end_reason = None  # e.g. "anti_crease_pattern"
        self._pending_tail_mean_w = None
        self._pending_tail_std_w = None
        self._pending_tail_peak_w = None
        # Stashed just before _transition_to_unemptied wipes confirmation (entity attrs +
        # confirm_entity selector) for "next load" - lets _recover_from_false_unemptied restore
        # this cycle's real confirmation if the Unemptied turns out to be false.
        self._pending_confirmed_by_user = None
        self._pending_confirmed_by = None

        # Delayed start (Miele delay timer) state - see detect_delayed_start config above
        self._delay_plateau_start = None       # UTC when current sub-start_w plateau began
        self._delayed_start_trimmed = False    # True once we've slid start_time past the wait
        self._delay_waiting = False            # True while ETA is paused for a suspected wait
        self._delayed_start_lead_idle_min = None  # Minutes trimmed off, for logging/diagnostics

        # Counters
        self.high_power_counter = 0
        self.low_power_counter = 0
        self.low_power_start_time = None  # Track when low power period started
        self.last_significant_power_at = None
        self.notification_sent = False

        # Programme classification (for adaptive finish detection)
        self.max_power_seen = 0.0         # Peak wattage observed during the current cycle
        self.observed_heating = False     # True once a >1000W heating phase is detected
        self.in_heating_phase = False     # Currently above 1000W (for phase counting)
        self.heating_phase_count = 0      # Number of distinct heating bursts seen
        self.detected_programme = "unknown"  # Classified programme (updated live)
        self.detected_temperature = None      # Classified or user-selected temperature

        # Programme confirmation & feedback learning
        self.confirm_entity = self.args.get("confirm_entity")
        self.temperature_entity = self.args.get("temperature_entity")  # optional, like spin
        self.spin_entity = self.args.get("spin_entity")  # optional: input_select for spin speed (rpm)
        # Optional: exact cycle end time from the user (input_datetime or input_text "HH:MM"). When set,
        # we use it for run_time_minutes and learning so we learn the true programme length.
        self.cycle_ended_at_entity = self.args.get("cycle_ended_at_entity")
        # Optional HA helpers for wash options (phase 1: store in feedback; phase 2: ETA adjustments).
        self.option_water_plus_entity = self.args.get("option_water_plus_entity")
        self.option_soak_entity = self.args.get("option_soak_entity")
        self.option_prewash_entity = self.args.get("option_prewash_entity")
        self.option_short_entity = self.args.get("option_short_entity")
        self.feedback_file = self.args.get(
            "feedback_file",
            "/data/appdaemon/apps/appliances/washer_feedback.json",
        )
        # If configured path does not exist, use path next to this app (e.g. /conf vs /data)
        if not os.path.exists(self.feedback_file):
            fallback = os.path.join(os.path.dirname(__file__), "washer_feedback.json")
            if os.path.exists(fallback):
                self.feedback_file = fallback
                self.log(f"Feedback file resolved to path next to app: {self.feedback_file}", level="DEBUG")
            # else: keep configured path for first run (will be created there)
        self.programme_confirmed_by_user = False  # True when user manually picked a programme
        self.confirmed_by_username: str | None = None  # HA person name who confirmed the programme (empty if UI gave no user_id)
        self._skip_next_confirm = False  # True when app is about to set confirm_entity (so we don't treat it as user confirmation)
        # Finish-guard duration bar. Seeded ("frozen") at the first confident classification,
        # then evidence-following: raises to any longer live classification, lowers only after
        # energy disproof + live-key stability (see wcls.resolve_guard_bar and _update_guard_bar;
        # 2026-08-11 silent-Off incident). Attribute name kept for entity-attr compatibility.
        self.expected_dur_at_start: float | None = None
        self._guard_bar_class: tuple | None = None  # (prog, temp) behind expected_dur_at_start; None = unknown (lower disabled)
        self._live_class_key: str | None = None     # "prog|temp" of the current live classification streak
        self._live_class_since = None               # UTC datetime when that streak began
        # Minutes the live classification must hold one key before it may LOWER the guard bar.
        self.guard_reclass_stable_minutes = float(self.args.get("guard_reclass_stable_minutes", 15.0))
        # Cumulative energy must exceed the bar programme's max energy by this factor to disprove it.
        self.guard_energy_disproof_margin = float(self.args.get("guard_energy_disproof_margin", 1.10))
        self._learned_durations: dict = {}        # {prog_key: avg_duration_min} from confirmed history
        self._history_centroids: dict = {}        # {prog_key: {rate, heating_bursts, n}} for pattern matching
        self._user_id_to_name: dict = {}          # {ha_user_id: person_name} built from person.* entities

        # Pause state tracking
        self.pause_timer = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False

        # Watchdog timers
        self.running_watchdog_timer = None
        self.unemptied_watchdog_timer = None
        self.unemptied_door_recheck_timer = None  # Periodic door check while Unemptied (catches missed open events)
        self.emptied_watchdog_timer = None  # Auto Off after emptied_timeout_minutes if door stays open
        self.power_unavailable_off_timer = None  # Grace period before forcing Off on plug/state dropout (2026-07-17)
        self._last_infer_start_attempt = None  # Throttle _infer_start_from_state_history
        self._last_finish_guard_info_log_at = None  # Throttle repetitive INFO finish-guard lines

        # Notification
        self.announce_message = self.args.get("announce_message", "Washer is ready to be emptied")
        self.announce_entity = self.args.get("announce_entity")  # input_boolean to enable/disable
        # If we only detect the finish this many minutes after it actually happened (finish
        # anchor = last_high_energy_at), a Sonos announcement is too late to be useful - send a
        # quieter mobile push ("finished ~N min ago") instead. See _transition_to_unemptied.
        # 20, not 15: the non-near-end finish path detects at ~16min (energy_stable_minutes 15 +
        # slack) and must stay on Sonos; genuinely-late detections run 30+min.
        self.announce_freshness_minutes = float(self.args.get("announce_freshness_minutes", 20))
        # Optional: announce when door *unlocks* instead of when we enter Unemptied.
        self.door_lock_entity = self.args.get("door_lock_entity")  # e.g. lock.washer_door

        # Settled per-cycle cost: meter the spot price against the cumulative energy sensor each
        # tick (see _check_energy_finish); the standby wait of a delayed start is excluded (reset
        # in _slide_start_for_delayed_start), same as duration.
        self.price_entity = self.args.get("price_entity", "sensor.energi_data_service")
        self.price_fallback_kr = float(self.args.get("price_fallback_kr", 1.7))
        self.track_cycle_cost = bool(self.args.get("track_cycle_cost", True))

        # Presence-gated confirm push: when a cycle ends unconfirmed but worth learning, ask
        # whoever is home to confirm the programme with one tap (see _maybe_send_confirm_push).
        self.confirm_push_enabled = bool(self.args.get("confirm_push_enabled", True))
        self.confirm_push_target = self.args.get("confirm_push_target", "home")
        # When the saved cycle record names who started it, target them directly instead of the
        # "home" broadcast (see _send_confirm_push) - MobileNotifier list-targets bypass
        # category_audience and reach them even after they've left. Flag-gated so this is
        # revertible without a code change.
        self.confirm_push_target_actor = bool(self.args.get("confirm_push_target_actor", True))
        self.confirm_push_dashboard_uri = self.args.get("confirm_push_dashboard_uri", "/local/ha-dashboard/index.html")

        # Get Sonos Notifier App instance
        self.sonos_notifier = None
        try:
            self.sonos_notifier = self.get_app("SonosNotifier")
            if self.sonos_notifier:
                self.log("Successfully got instance of SonosNotifier.", level="INFO")
            else:
                self.log("WARN: SonosNotifier app not found.", level="WARNING")
        except Exception as e:
            self.log(f"WARN: Error getting SonosNotifier app: {e}", level="WARNING")

        # Dead-plug watchdog: unlike the dishwasher there is no Error state here - an
        # unavailable plug forces Off immediately (_handle_unavailable), so a dead Shelly
        # is indistinguishable from an idle washer. After this grace, page the phone; one
        # push per outage + all-clear on recovery (gw2000a_watchdog policy: dead sensor =
        # maintenance to act on, not house-feed material).
        self.plug_outage_push_after_seconds = int(self.args.get("power_unavailable_push_after_seconds", 180))
        self.notify_target = self.args.get("notify_target", ["mikkel"])
        self._plug_outage_push_timer = None
        self._plug_outage_pushed = False

        self._build_user_id_cache()

        # Durable cycle store: HA erases sensor.washer_state (an AppDaemon set_state entity)
        # on every HA core restart, taking the cycle clock in its attributes with it; this
        # on-disk shadow copy survives that (see cycle_store.py's module docstring). Filename
        # MUST end in _state.json - .gitignore excludes *_state.json and deploy.sh only rsyncs
        # git-tracked files, so a runtime cycle-state file can never become tracked. See
        # cycle_persistence.py's _init_cycle_store for why the default path is built here, not
        # there.
        self._init_cycle_store(
            appliance="washer",
            default_path=Path(__file__).with_name("washer_cycle_state.json"),
        )
        # Reject a stored Running/Paused whose save is older than this - AppDaemon (or the box)
        # was down long enough that trusting a frozen clock is riskier than the normal
        # power-based re-detection (see the boot-resolution staleness check below).
        self.store_max_downtime_hours = float(self.args.get("store_max_downtime_hours", 12))
        # Small clock-skew allowance for the future-start_time rejection (see
        # _resolve_store_candidate) - NTP may not have synced yet at boot, so a start_time a few
        # minutes ahead of "now" is tolerated; anything further ahead is rejected outright.
        self.store_future_skew_minutes = float(self.args.get("store_future_skew_minutes", 5))
        # A Running/Paused state restored from a NON-live source (durable store, helper+history)
        # counts as corroborated only if current power is above finish_standby_max_watts or
        # last_high_energy_at is within this window of now; otherwise the announcement is
        # suppressed and a one-shot reconcile runs ~60s later (see the restore corroboration
        # below, _restore_reconcile and _power_changed's clear).
        self.restore_corroboration_window_minutes = float(self.args.get("restore_corroboration_window_minutes", 10))
        self._cycle_id = None  # uuid4, minted below/in _begin_running_cycle - notification_sent's own scoping is on _start_time_source == "durable_store", see _finalize_restored_cycle_identity
        # state_since for the durable store - deliberately its own field, not last_state_change -
        # see cycle_persistence.py's _build_cycle_store_payload for why last_state_change itself
        # is never persisted.
        self._store_state_since = None
        # FIX 1 (2026-08-19): set True when a Running/Paused restore came from a non-live source
        # and no live signal corroborated it yet - suppresses announcements until either a fresh
        # power sample above start_w clears it (_power_changed) or the one-shot reconcile
        # concludes the cycle quietly (_restore_reconcile). Default False on every other path.
        self.restored_uncorroborated = False
        self._restore_reconcile_timer = None
        # Rate-limits the Unemptied door-history reconciler (FIX 4) to ~5 min between recorder
        # queries rather than one per 60s tick - see _unemptied_door_recheck.
        self._unemptied_last_history_check_at = None
        # D1 (2026-08-19): set only for the duration of _restore_reconcile's own transition call -
        # see _finish_anchor and _restore_reconcile's docstring invariant. None everywhere else.
        self._finish_anchor_override = None
        # D2 (2026-08-19 adversarial pass follow-up): set only for the duration of
        # _restore_reconcile's own transition call, same lifecycle as _finish_anchor_override
        # above - forces the announce block's push branch explicitly, rather than relying on the
        # freshness-latency arithmetic to always exceed announce_freshness_minutes (it doesn't,
        # once the yaml knobs are retuned - see _restore_reconcile's docstring). False everywhere
        # else.
        self._announce_force_push = False

        # Restore previous state. Read the entity's bare state, its full attribute snapshot,
        # and the on-disk store ALL ONCE, here, before any write - _set_state_entity below is
        # the first write this boot, and on 2026-07-27 a restore read the entity back AFTER
        # that write had already recreated it with no attributes, so cycle_start_time read back
        # as None forever - see cycle_persistence.py's _capture_cycle_store_boot_snapshot.
        existing, boot_full, store_data = self._capture_cycle_store_boot_snapshot()            # A/B/C
        boot_full = boot_full or {}
        entity_missing = existing in (None, "unknown", "unavailable")
        if entity_missing:
            # HA erased this AppDaemon set_state entity (restart) and we're about to recreate
            # it below (directly, or seeded from ui_state_select/the durable store). Once
            # recreated, its last_changed means "AppDaemon recreated this", not a real cycle
            # transition - stamp the time so later start_time corrections can tell the two
            # apart (see the _entity_recreated_at guard around the "entity last_changed"
            # heuristic).
            self._entity_recreated_at = self._now_utc()
        boot_attrs = dict(boot_full.get("attributes") or {})
        boot_last_changed = boot_full.get("last_changed") or boot_full.get("last_updated")

        valid_states = ("Running", "Unemptied", "Paused", "Emptied")
        entity_trusted = existing in valid_states
        # Any valid state may now be seeded from the mirror. Previously only the clock-free
        # states (Unemptied/Emptied) were seedable: Running/Paused live off cycle_start_time,
        # which lived in the erased sensor's attributes and was gone with it, so seeding them
        # used to yield Running with start_time None - every timer that could end the cycle is
        # gated on start_time, so the machine sat in Running forever (_confirm_finished saw
        # run_minutes 0 and never cleared the duration guards). The durable store (and, failing
        # that, a power+history recovery below) now gives Running/Paused an independent clock,
        # so the restriction is replaced by a mechanically-checkable invariant instead:
        # self.state is only ever left as Running/Paused in a branch that already holds a
        # non-None start_time (see the gate below) - if no trustworthy start_time is
        # recoverable from any source, we fall through to Off, exactly like an unseedable state
        # always did.
        seedable_states = valid_states
        seeded_from_helper = False
        helper_state = None
        if entity_missing and self.ui_state_select:
            helper_state = self.get_state(self.ui_state_select)                                # D

        try:
            boot_watts = float(self.get_state(self.power_sensor) or 0)                         # E
        except (ValueError, TypeError):
            boot_watts = 0.0

        # ----- F: resolve state name + start_time + fields (pure - no entity writes yet) -----
        # Precedence when the entity itself has nothing usable: durable store, then the mirror,
        # then Off. Store beats helper because the write order is store -> entity -> helper (see
        # _maybe_persist_cycle_state / _save_cycle_state) - by the time the mirror could reflect
        # a DIFFERENT state, the store write for the entity's actual state already happened.
        resolved_state = existing if entity_trusted else None
        start_time = None
        store_used = False
        restore_attrs = None
        restore_last_changed = boot_last_changed if entity_trusted else None
        if not entity_trusted:
            try:
                store_state, store_start = self._resolve_store_candidate(store_data)
                if store_state is not None:
                    resolved_state = store_state
                    start_time = store_start
                    store_used = True
                    self._start_time_source = "durable_store"
                    restore_attrs = self._store_data_to_entity_attrs(store_data)
                    if helper_state in valid_states and helper_state != store_state:
                        self.log(
                            f"Boot: durable store says {store_state!r} but {self.ui_state_select} "
                            f"says {helper_state!r} - store wins (write order is store -> "
                            f"entity -> helper)",
                            level="WARNING",
                        )
                elif store_data and store_data.get("state") in ("Unemptied", "Emptied"):
                    # Clock-free states: _resolve_store_candidate above only ever validates
                    # Running/Paused (they need a start_time; these two don't), so a store
                    # record for either lands here instead. Resolving the bare name is all that
                    # is needed - the "elif self.state == 'Unemptied'/'Emptied'" branches below
                    # already re-arm their own watchdogs unconditionally on self.state, exactly
                    # as they already do for an AD-only reload or a helper-seed. No staleness
                    # gate (unlike Running/Paused's start_time/saved_at checks above): these
                    # states carry no clock to go stale, matching today's unconditional
                    # helper-seed for the very same two states. Without this branch, a
                    # store-only Unemptied/Emptied (mirror missing or disagreeing) fell through
                    # all the way to Off, and that Off's own publish then cleared the very
                    # record it had just failed to use.
                    resolved_state = store_data.get("state")
                    store_used = True
                    if helper_state in valid_states and helper_state != resolved_state:
                        self.log(
                            f"Boot: durable store says {resolved_state!r} but {self.ui_state_select} "
                            f"says {helper_state!r} - store wins (write order is store -> "
                            f"entity -> helper)",
                            level="WARNING",
                        )
                elif helper_state in seedable_states:
                    resolved_state = helper_state
                    seeded_from_helper = True
                    self.log(
                        f"State seeded from {self.ui_state_select} - sensor was missing (HA restart?)",
                        level="INFO",
                    )
                    if resolved_state in ("Running", "Paused") and boot_watts >= self.start_w:
                        # No durable clock for this seed - last resort: the same state/power
                        # history the legacy power-is-truth recovery below uses, run early
                        # enough to inform the gate (start_time must already be known before
                        # _restore_running_state is even called - see the ordering rule above).
                        start_time, source = self._infer_boot_start_time_from_history()
                        if start_time is not None:
                            self._start_time_source = source
                # No "elif helper_state in valid_states" here: seedable_states IS valid_states
                # (see above), so the seedable branch already covers every valid helper_state -
                # the old "not seeding, power detection re-establishes it" branch was dead.
            except Exception as e:
                # Every store interaction (and the boot resolution hanging off it) must be
                # non-fatal - a corrupt store degrades to today's plain restore, never kills
                # the app.
                self.log(f"Boot resolve raised unexpectedly: {e} - falling back to plain restore", level="WARNING")
                resolved_state = None
                start_time = None
                store_used = False
                restore_attrs = None

        self.state = resolved_state if resolved_state in valid_states else "Off"

        # Legacy power-is-truth recovery: entity/store/helper all landed on "Off" but power
        # says the machine is actively drawing start current. If we skip restore and bootstrap
        # calls _confirm_running, we wipe start_time and user context.
        if self.state == "Off" and boot_watts >= self.start_w:
            self.log(
                f"Initialize: {self.state_entity} is Off but power is {boot_watts:.0f}W "
                f"(>= {self.start_w:.0f}W) - treating as restart during an active wash; forcing Running",
                level="WARNING",
            )
            self.state = "Running"
            if start_time is None:
                start_time, source = self._infer_boot_start_time_from_history()
                if start_time is not None:
                    self._start_time_source = source

        # ----- G: gate -----
        if not entity_trusted and self.state in ("Running", "Paused") and start_time is None:
            self.log(
                f"Initialize: resolved {self.state} with no recoverable start_time (no live "
                f"entity clock, no usable durable store, no power/history corroboration) - "
                f"falling through to Off rather than leaving finish detection permanently gated",
                level="WARNING",
            )
            self.state = "Off"
            store_used = False
            restore_attrs = None
            seeded_from_helper = False
            self._start_time_source = None
        elif not entity_trusted and self.state in ("Running", "Paused"):
            self.start_time = start_time
            if restore_attrs is None:
                # Helper-seeded + inferred, or the legacy power-is-truth recovery: preserve
                # whatever the (non-erased, literally "Off") entity's attributes already
                # carried (e.g. last_off_at, last_door_closed_at from a real previous Off) -
                # same idea as the old recovery_off_to_running merge - and stamp in the clock
                # just resolved above.
                restore_attrs = dict(boot_attrs)
            restore_attrs["cycle_start_time"] = self._format_utc(self.start_time)
            restore_attrs["cycle_start_time_local"] = self._format_local(self.start_time)
            restore_attrs["started_at_display"] = self.start_time.astimezone(self._local_tz()).strftime("%H:%M")

        # Restore _store_state_since (see the mixin's _build_cycle_store_payload docstring) so
        # it survives multiple consecutive restarts instead of resetting to "now" every boot:
        # prefer the entity's own last_changed when it is itself live (AD-only reload -
        # boot_last_changed is always None after a HA-restart erasure, so this is meaningless
        # there), else the store's own state_since when THIS boot's resolution actually came
        # from the store (store_used - never from an unrelated store record that lost out to
        # the mirror or was rejected as stale). Only when a real value is found is
        # _store_last_written_state also set to the resolved self.state, so the imminent first
        # write below does not mistake this restart for a fresh transition and re-stamp
        # state_since to "now" anyway (see _cycle_store_before_save); when nothing restorable is
        # found, _store_last_written_state is left alone and that first write's own "now" stamp
        # (the pre-existing behaviour) stands, same as it always has.
        if self.state != "Off":
            restored_since = None
            if entity_trusted and boot_last_changed:
                restored_since = cystore.parse_utc(boot_last_changed)
            elif store_used and store_data:
                restored_since = cystore.parse_utc(store_data.get("state_since"))
            if restored_since is not None:
                self._store_state_since = restored_since
                self._store_last_written_state = self.state

        # Only publish when state differs. Re-sending the same state can strip attributes on
        # some HA/AppDaemon setups. (seeded_from_helper/store_used always publish: the state
        # entity itself is still missing/unknown, or literally disagreed with what we resolved.)
        if self.state != existing or seeded_from_helper:                                       # H
            if self.state in ("Running", "Paused"):
                self._set_state_entity(state=self.state, attributes=restore_attrs, replace=True)
            else:
                self._set_state_entity(state=self.state)

        # Restore in-memory state from persisted attributes so ETA, energy-used, "user
        # confirmed programme", etc. survive an app reload, an HA restart (durable store), or a
        # missed transition (power-is-truth). Paused now shares this path with Running - both
        # read from the SAME attrs snapshot below, and the 5h running watchdog spans Paused
        # time too (never cancelled on pause - see _transition_to_paused), so a restored Paused
        # needs the same timer re-arm Running always got; previously it got none at all.
        if self.state in ("Running", "Paused"):
            self._restore_running_state(                                                       # I
                attrs=restore_attrs if restore_attrs is not None else boot_attrs,
                last_changed=restore_last_changed,
            )
            # Post-condition safety net on top of the gate above: _restore_running_state's
            # correction paths are complex enough, and shared with the live-tick code, that a
            # defensive last-resort is worth more here than treating "unreachable" as a promise
            # (e.g. a corrupt cycle_start_time on an otherwise-trusted live entity).
            if self.start_time is None:
                self.log(
                    "Initialize: start_time was lost while restoring - falling through to Off "
                    "rather than leaving finish detection permanently gated",
                    level="WARNING",
                )
                self._transition_to_off("Restore: lost start_time during restore", force=True)
            else:
                self._finalize_restored_cycle_identity(store_data if store_used else None)
                # force=True: unconditional, matching this call's original always-write
                # behaviour (see the mixin's _save_cycle_state) - the FIRST write this boot (a
                # few lines up) necessarily persisted a snapshot from before this restore
                # dispatch populated start_time/energy_start/etc, so this makes the on-disk
                # payload complete immediately rather than waiting for a naturally-throttled one.
                self._save_cycle_state(force=True)                                              # J
                # FIX 1 (2026-08-19): a Running/Paused restored from a NON-live source (durable
                # store, helper+history inference) is only a HYPOTHESIS until a live signal backs
                # it. The 2026-08-19 incident restored a stale Running record onto a machine that
                # had already finished and been emptied, at 0W, then announced Unemptied minutes
                # after the user emptied it. Corroborate: current power above the standby ceiling,
                # or last_high_energy_at within restore_corroboration_window_minutes of now. A
                # store payload written by a DIFFERENT code fingerprint is treated as
                # uncorroborated too (its semantic fields may no longer mean the same thing). The
                # live-entity path (entity_trusted) and the legacy power-is-truth force path are
                # corroborated by construction - power there is >= start_w > the ceiling - so they
                # never trip this. Uncorroborated: keep the state and clock (quiet mid-cycle
                # phases are real - do NOT drop to Off), but suppress announcements and schedule a
                # one-shot reconcile (_restore_reconcile); a fresh sample >= start_w clears it.
                if not entity_trusted:
                    recent_high = (
                        self.last_high_energy_at is not None
                        and (self._now_utc() - self.last_high_energy_at)
                        <= timedelta(minutes=self.restore_corroboration_window_minutes)
                    )
                    power_ok = boot_watts > self.finish_standby_max_watts
                    stored_fp = (store_data or {}).get("code_fingerprint") if store_used else None
                    fingerprint_mismatch = (
                        stored_fp is not None
                        and getattr(self, "_code_fingerprint", None) is not None
                        and stored_fp != self._code_fingerprint
                    )
                    if (power_ok or recent_high) and not fingerprint_mismatch:
                        self.restored_uncorroborated = False
                    else:
                        self.restored_uncorroborated = True
                        why = "store code fingerprint changed" if fingerprint_mismatch else (
                            f"power {boot_watts:.0f}W <= {self.finish_standby_max_watts:.0f}W and no "
                            f"high-energy sample within {self.restore_corroboration_window_minutes:.0f}min"
                        )
                        self.log(
                            f"Restore: {self.state} from a non-live source is UNCORROBORATED "
                            f"({why}) - suppressing announcements, reconciling in 60s",
                            level="WARNING",
                        )
                        self._restore_reconcile_timer = self.run_in(self._restore_reconcile, 60)
        elif self.state == "Unemptied":
            # Restart door-recheck and watchdog timers so we don't get stuck in Unemptied after
            # an app reload (the timers are not persisted across restarts).
            if not self.unemptied_door_recheck_timer:
                self.unemptied_door_recheck_timer = self.run_in(self._unemptied_door_recheck, 60)
            if not self.unemptied_watchdog_timer:
                self.unemptied_watchdog_timer = self.run_in(
                    self._unemptied_watchdog_timeout,
                    int(self.unemptied_timeout_hours * 3600),
                )
            # D1 (2026-08-19 adversarial pass): restore last_high_energy_at and start_time from
            # the store so _finish_anchor() has a real floor for the FIX-4 door-history recheck,
            # instead of collapsing to "now - anti_crease_window_minutes" (8min) and missing an
            # older ajar emptying. Scoped to exactly these two fields - no other Running-side
            # restore logic runs for Unemptied. Only when THIS boot's Unemptied resolution
            # actually came from the store (store_used - never from an unrelated/stale record,
            # matching the same guard the Running/Paused branch above uses). When the payload
            # lacks them, _finish_anchor()'s _store_state_since fallback (step c, restored
            # separately just above) still covers it.
            if store_used and store_data:
                restored_high = cystore.parse_utc(store_data.get("last_high_energy_at"))
                if restored_high is not None:
                    self.last_high_energy_at = restored_high
                restored_start = cystore.parse_utc(store_data.get("start_time"))
                if restored_start is not None:
                    self.start_time = restored_start
        elif self.state == "Emptied":
            # Check if power is already 0W - machine is off, go directly to Off.
            try:
                current_watts = float(self.get_state(self.power_sensor) or 0)
                if current_watts <= 0:
                    self.log("Restore: Emptied state with 0W - transitioning to Off", level="INFO")
                    self._transition_to_off("Restore: Emptied + 0W - machine off")
            except (ValueError, TypeError):
                pass
            # If not at 0W (or check failed), start the emptied watchdog as fallback.
            if self.state == "Emptied" and not self.emptied_watchdog_timer:
                self.emptied_watchdog_timer = self.run_in(
                    self._emptied_watchdog_timeout,
                    int(self.emptied_timeout_minutes * 60),
                )

        # Boot-only snapshot: drop it now so any future call to a restore-flavoured helper
        # falls back to a live read instead of replaying this boot's stale snapshot - see
        # cycle_persistence.py's _boot_full_state_snapshot. Nothing in this file currently
        # reads it back (unlike dishwasher_monitor.py's _handle_force_emptied), but dropping it
        # here keeps that guarantee mechanical rather than incidental for this monitor too.
        self._drop_cycle_store_boot_snapshot()

        # Listen for events
        self.listen_state(self._handle_unavailable, self.state_entity, new="unavailable")
        self.listen_state(self._handle_unavailable, self.power_sensor, new="unavailable")
        self.listen_state(self._power_changed, self.power_sensor)
        self.listen_state(self._door_state_changed, self.door_sensor)
        if self.door_lock_entity:
            self.listen_state(self._door_lock_state_changed, self.door_lock_entity)
        if self.confirm_entity:
            self.listen_state(self._on_confirm_changed, self.confirm_entity)
        if self.temperature_entity:
            self.listen_state(self._on_confirm_changed, self.temperature_entity)
        if self.vibration_sensor:
            self.listen_state(self._vibration_changed, self.vibration_sensor)

        # Presence-gated confirm push: button presses on the actionable notification, and a
        # test hook to preview the message/buttons on demand (see _send_confirm_push).
        self.listen_event(self._on_confirm_push_action, "mobile_app_notification_action")
        self.listen_event(self._on_test_confirm_push, "washer_test_confirm_push")
        # Dashboard "Emptied" button (2026-08-07) - same convention as dishwasher_force_emptied.
        self.listen_event(self._handle_force_emptied, "washer_force_emptied")

        # Load historical feedback and derive learned duration estimates
        self._load_and_apply_feedback()

        # Optional: one-time migration to add completion_class / valid_for_learning (idempotent, flag-gated).
        if self.args.get("run_feedback_migration"):
            dry_run = self.args.get("feedback_migration_dry_run", True)
            self._migrate_feedback_add_completion_class(dry_run=dry_run)

        # Ensure programme input_select has correct options (programme name only).
        # Build from YAML/profile display order; HA does not persist set_options so we re-apply each load.
        if self.confirm_entity:
            try:
                order = getattr(self, "_programme_display_order", None) or list(self._PROGRAMME_DISPLAY_ORDER)
                prog_options = ["Auto (unconfirmed)"]
                for key in order:
                    if key in self.PROGRAMME_PROFILES and key != "unknown":
                        label = self.PROGRAMME_PROFILES[key].get("label", key)
                        prog_options.append(label)
                self.call_service(
                    "input_select/set_options",
                    entity_id=self.confirm_entity,
                    options=prog_options,
                )
                # If a programme is already selected (e.g. after restart), constrain temp/spin to that programme
                try:
                    current_label = self.get_state(self.confirm_entity)
                    if current_label and current_label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                        prog_key = self._LABEL_TO_KEY.get(current_label)
                        if prog_key and prog_key != "unknown":
                            self._apply_programme_ui_dropdowns(prog_key)
                except Exception:
                    pass
            except Exception as e:
                self.log(f"Could not set programme selector options: {e}", level="DEBUG")

        # Bootstrap
        current_power = self.get_state(self.power_sensor)
        if current_power not in ["unknown", "unavailable"]:
            try:
                current_watts = float(current_power or 0)
                self._power_changed(self.power_sensor, None, None, current_watts, {})
            except (ValueError, TypeError):
                self._handle_unavailable(self.power_sensor, None, None, current_power, {})
        else:
            # Plug already dead at app start: the unavailable listener will never fire
            # (no transition), so arm the dead-plug watchdog here (dishwasher does the
            # same in its bootstrap).
            self._begin_plug_outage_grace()

        self.log(f"WasherMonitor (Miele WEA 035 WCS) initialized - state: {self.state}", level="INFO")

    def _resolve_store_candidate(self, store_data):
        """Validate a loaded CycleStore payload as a boot-time Running/Paused candidate.

        Returns (state, start_time) for a fresh, plausible record; (None, None) for anything
        else - missing, some other state (Unemptied/Emptied/Off - out of scope here; a
        store-only Unemptied/Emptied is instead resolved by a sibling branch in initialize(),
        since neither needs a clock), an unparsable clock, a start_time implausibly in the
        future, or stale by either downtime measure - see
        cycle_persistence.py's _cycle_store_validate_running_candidate, which does the actual
        staleness/future-clock check shared with the dryer and dishwasher. Each rejection logs
        exactly one WARNING. Never raises.
        """
        if not store_data or store_data.get("state") not in ("Running", "Paused"):
            return None, None
        state = store_data.get("state")
        start_time = cystore.parse_utc(store_data.get("start_time"))
        now = self._now_utc()
        saved_at = cystore.parse_utc(store_data.get("saved_at"))
        ok = self._cycle_store_validate_running_candidate(
            state=state,
            start_time=start_time,
            saved_at=saved_at,
            now=now,
            # store_future_skew_minutes (not boot_future_start_skew_s, dryer/dishwasher's own
            # arg name/unit) - a real difference in tolerance (5min default here vs. 60s there),
            # not just spelling, so converted to seconds here rather than normalised away.
            future_skew_s=self.store_future_skew_minutes * 60,
            max_running_hours=self.max_running_hours,
            max_downtime_hours=self.store_max_downtime_hours,
        )
        if not ok:
            return None, None
        return state, start_time

    def _infer_boot_start_time_from_history(self):
        """Pure variant of _infer_wash_start_time_when_missing: same two-step fallback chain
        (state-entity transition history, then sustained high power in the power sensor's own
        history), but returns the candidate instead of assigning it to self.start_time and
        pushing it to the entity. Used during initialize()'s boot resolution, before
        self.start_time is known - the ordering rule at the top of initialize() forbids any
        entity write until every boot-resolution read is done, and
        _infer_wash_start_time_when_missing writes immediately on success.

        Returns (start_time, source) - source is "state_history" or "power_history", matching
        the _START_TIME_SOURCE_RANK keys those two signals already use elsewhere in this file -
        or (None, None) when neither finds anything.
        """
        inferred = self._infer_start_from_state_history()
        if inferred is not None:
            return inferred, "state_history"
        inferred = self._infer_first_sustained_high_power_start(hours=8)
        if inferred is not None:
            return inferred, "power_history"
        return None, None

    def _store_data_to_entity_attrs(self, store_data) -> dict:
        """Translate a durable-store payload into the entity-attribute shape
        _restore_running_state already knows how to read (e.g. start_time -> cycle_start_time,
        heating_phase_count -> heating_bursts), so the store-restore path reuses that
        function's existing logic byte-for-byte instead of a second, parallel implementation.
        cycle_start_time itself is added by the caller once self.start_time is finalized.
        """
        attrs = {}
        for store_key, attr_key in (
            ("last_door_closed_at", "last_door_closed_at"),
            ("last_door_closed_trusted", "last_door_closed_trusted"),
            ("last_off_at", "last_off_at"),
            ("energy_at_start", "energy_at_start"),
            ("started_by", "started_by"),
            ("started_by_method", "started_by_method"),
            ("session_cost_kr", "session_cost_kr"),
            ("detected_programme", "detected_programme"),
            ("detected_temperature", "detected_temperature"),
            ("expected_dur_at_start", "expected_dur_at_start"),
            ("expected_dur_key", "expected_dur_key"),
            ("programme_confirmed_by_user", "programme_confirmed_by_user"),
            ("programme_confirmed_by", "programme_confirmed_by"),
            ("delayed_start_trimmed", "delayed_start_trimmed"),
            ("last_high_energy_at", "last_high_energy_at"),
            ("heating_phase_count", "heating_bursts"),
        ):
            value = store_data.get(store_key)
            if value not in (None, ""):
                attrs[attr_key] = value
        return attrs

    def _finalize_restored_cycle_identity(self, store_data):
        """Resolve _cycle_id / notification_sent - and the store-only per-cycle counters
        _restore_running_state never reads (heating_phase_count, max_power_seen,
        finish_confirmed, in_finishing_tail and its tail-pulse/tail-pattern fields,
        door_opened_during_cycle) - once start_time is finally settled, right after
        _restore_running_state returns.

        store_data is the durable-store payload IF this boot restored from it, else None
        (AD-reload, helper-seed+inference, or the legacy power-is-truth recovery - none of
        those have a prior cycle identity to inherit). When store_data is given, it is only
        trusted while self._start_time_source is still "durable_store": the power-history
        cross-check inside _restore_running_state (unconditional, no rank gate - see
        _start_time_rank) may have judged the store's clock too stale and corrected it to a
        later, inferred start ("previous-cycle contamination"). At that point the store was
        tracking a DIFFERENT cycle than the one actually being restored, and none of its
        per-cycle counters may carry over: a stale notification_sent=True must never suppress a
        real announcement for what is, in every way that matters, a new cycle.
        """
        if store_data is not None and self._start_time_source == "durable_store":
            self._cycle_id = store_data.get("cycle_id") or str(uuid.uuid4())
            self.notification_sent = bool(store_data.get("notification_sent"))
            self.heating_phase_count = int(store_data.get("heating_phase_count") or 0)
            self.max_power_seen = float(store_data.get("max_power_seen") or 0.0)
            if store_data.get("observed_heating"):
                self.observed_heating = True
            self.finish_confirmed = bool(store_data.get("finish_confirmed"))
            self.in_finishing_tail = bool(store_data.get("in_finishing_tail"))
            self.in_finishing_tail_entered_at = cystore.parse_utc(store_data.get("in_finishing_tail_entered_at"))
            self.last_tail_pulse_at = cystore.parse_utc(store_data.get("last_tail_pulse_at"))
            self.tail_pattern_locked = bool(store_data.get("tail_pattern_locked"))
            self.tail_pattern_cycle_seconds = store_data.get("tail_pattern_cycle_seconds")
            self.tail_pattern_last_pulse_at = cystore.parse_utc(store_data.get("tail_pattern_last_pulse_at"))
            self.tail_pattern_locked_at = cystore.parse_utc(store_data.get("tail_pattern_locked_at"))
            self.door_opened_during_cycle = bool(store_data.get("door_opened_during_cycle"))
        else:
            if store_data is not None:
                self.log(
                    "Restore: power/history evidence moved the stored start_time - treating this "
                    "as a fresh cycle identity (heating/tail/notification counters not carried over)",
                    level="INFO",
                )
            self._cycle_id = str(uuid.uuid4())
            self.notification_sent = False

    def _restore_reconcile(self, kwargs):
        """One-shot ~60s after an uncorroborated Running/Paused restore (FIX 1b; scheduled in
        initialize()). If the machine has stayed at/below standby the whole visible window and
        the run is already a real cycle length, the wash ended while we were down - conclude it:
        _correct_duration + a single feedback save, routed to Emptied if the door came into play
        (FIX 2) or Unemptied announced by the FRESHNESS gate only (the flag is cleared below, so
        the huge detection latency downgrades Sonos to a mobile push - wet laundry must be late
        but never silent). Otherwise leave it Running - a fresh
        sample >= start_w (_power_changed) clears the flag and the cycle continues normally, and
        a genuine later finish announces then. Concludes on evidence only: absent power history
        is never taken as proof the machine is off.

        D1 invariant (2026-08-19 adversarial pass): while the transition below runs,
        _finish_anchor_override pins the finish anchor to start_time + addload_window_minutes -
        never last_high_energy_at, which on this path may be a synthetic boot-seeded placeholder
        (not a fact of when the wash actually finished) or explicitly distrusted (code-fingerprint
        mismatch). start_time is guaranteed non-None by the gate above.

        D2 invariant (2026-08-19 adversarial pass follow-up): the "never Sonos" guarantee on this
        path used to rest on arithmetic alone - freshness latency (>= run_minutes -
        addload_window_minutes, and run_minutes >= min_cycle_minutes here) happened to always
        exceed announce_freshness_minutes only because the three yaml knobs' *default* values
        satisfy min_cycle_minutes - addload_window_minutes >= announce_freshness_minutes. Retuning
        any one of them breaks that inequality and silently re-enables Sonos on a boot-time
        conclusion. _announce_force_push below removes the reliance on arithmetic: it is set True
        for the duration of the transition call (same lifecycle as _finish_anchor_override) and
        makes the announce block's push branch fire unconditionally, so Sonos is impossible on the
        reconcile path by construction - only the door-gate (Emptied, silent) or the forced push
        gate (mobile push) can fire, regardless of how the knobs are tuned."""
        self._restore_reconcile_timer = None
        if not self.restored_uncorroborated:
            return
        if self.state not in ("Running", "Paused"):
            return
        if self.start_time is None:
            return
        run_minutes = (self._now_utc() - self.start_time).total_seconds() / 60
        if run_minutes < self.min_cycle_minutes:
            return
        points = self._get_recent_power_history(self.restore_corroboration_window_minutes)
        if not points:
            return
        if any(w > self.finish_standby_max_watts for _, w in points):
            return
        self.log(
            f"Restore reconcile: uncorroborated {self.state} has stayed <= "
            f"{self.finish_standby_max_watts:.0f}W for the last "
            f"{self.restore_corroboration_window_minutes:.0f}min (run {run_minutes:.0f}min) - "
            f"cycle ended while we were down; concluding quietly",
            level="INFO",
        )
        # standby_backstop (a known transition path that also skips the power-pattern gate): the
        # reconcile has itself verified sustained standby, so this is a boot-time variant of the
        # same "finished on sustained 0W" ending.
        self._pending_end_reason = "standby_backstop"
        # The reconcile IS the detection event: clear the suppression so the announce gate runs.
        # The door-gate (FIX 2) still routes to Emptied silently when someone already emptied;
        # otherwise the freshness gate sees the large latency and sends the mobile push, never
        # Sonos. Without this, a wash finishing during an HA outage ended in total silence.
        self.restored_uncorroborated = False
        # D1: pin the finish anchor for the duration of this transition only - see the docstring
        # invariant above. Cleared in finally so a later, genuinely-live finish is never anchored
        # to this boot-time value.
        self._finish_anchor_override = self.start_time + timedelta(minutes=self.addload_window_minutes)
        # D2: force the announce block's push branch explicitly, rather than trusting the
        # freshness-latency arithmetic to always win - see the docstring invariant above. Cleared
        # in finally, same lifecycle as _finish_anchor_override.
        self._announce_force_push = True
        try:
            self._transition_to_unemptied(force=True)
        finally:
            self._finish_anchor_override = None
            self._announce_force_push = False

    def _restore_running_state(self, attrs=None, last_changed=None):
        """Restore in-memory state when we were Running or Paused before a restart.

        Every read below comes from `attrs` (a snapshot of the state entity's attributes -
        either the still-live entity for an AppDaemon-only reload, or synthesized from the
        durable store when HA erased the entity - see initialize()'s boot resolution) and
        `last_changed`, never a fresh get_state() call: 2026-07-27 shipped a version that
        recreated the erased entity first and then read cycle_start_time back from that same
        just-created (attribute-less) entity, so start_time stayed None and every timer that
        could end the cycle was gated on it forever. When called with attrs=None (e.g. a
        standalone call in a test), both default to a fresh snapshot of the entity, matching
        the old always-read-live behaviour exactly.

        Reads cycle_start_time, energy_at_start, detected_programme from that snapshot (we
        persist these while Running/Paused). Also sets programme_confirmed_by_user if the user
        had selected a programme in the confirm dropdown. Restarts energy-check and watchdog
        timers so finish detection continues.
        """
        if attrs is None:
            full = self.get_state(self.state_entity, attribute="all") or {}
            attrs = full.get("attributes") or {}
            if last_changed is None:
                last_changed = full.get("last_changed") or full.get("last_updated")

        try:
            start_str = attrs.get("cycle_start_time")
            if start_str:
                self.start_time = _parse_utc(start_str)
                if self.start_time:
                    self.log(f"Restored cycle start time: {start_str}", level="DEBUG")
        except (TypeError, ValueError, AttributeError) as e:
            self.log(f"Could not restore cycle_start_time: {e}", level="DEBUG")

        # last_door_closed_at on the entity may be from before we stored trust, or wrong; only use when trusted.
        self.last_door_closed_trusted = False
        try:
            trusted_attr = attrs.get("last_door_closed_trusted")
            trusted = self._attr_bool_true(trusted_attr)
            last_door_str_entity = attrs.get("last_door_closed_at")
            if trusted and last_door_str_entity:
                ld = _parse_utc(last_door_str_entity)
                if ld:
                    self.last_door_closed_at = ld
                    self.last_door_closed_trusted = True
                    self.log(f"Restored trusted last_door_closed_at: {last_door_str_entity}", level="DEBUG")
            elif last_door_str_entity and not trusted:
                self.log(
                    "Restore: ignoring persisted last_door_closed_at (not trusted - e.g. HA restart or legacy entity)",
                    level="INFO",
                )
        except (TypeError, ValueError, AttributeError):
            pass

        # If we restored an old cycle_start_time (e.g. HA hadn't applied our set_state before restart),
        # clamp to after last Off / last trusted door close so we never show a start time from a previous cycle.
        try:
            last_off_str = attrs.get("last_off_at")
            if self.start_time:
                clamp_to = None
                if last_off_str:
                    last_off = _parse_utc(last_off_str)
                    if last_off and self.start_time < last_off:
                        clamp_to = last_off
                if self.last_door_closed_trusted and self.last_door_closed_at:
                    last_door = self.last_door_closed_at
                    if self.start_time < last_door:
                        if clamp_to is None or last_door > clamp_to:
                            clamp_to = last_door
                if clamp_to is not None:
                    gap_seconds = (clamp_to - self.start_time).total_seconds()
                    # Use >= so a stale value exactly pause_window (e.g. 10 min) before last_off gets clamped
                    if gap_seconds >= self.pause_window_minutes * 60:
                        self.log(
                            f"Restore: clamping start_time to after last Off/door close "
                            f"(was {self._strftime_local(self.start_time)}, HA had stale value before restart)",
                            level="INFO",
                        )
                        self.start_time = clamp_to
                        # Push corrected start time to HA; preserve existing attributes (set_state replaces attrs)
                        self._push_corrected_start_time_to_entity()
        except (TypeError, ValueError, AttributeError):
            pass

        # Restore: if start_time is still before when we went to Running (entity last_changed), use last_changed.
        # Skip when we have a trusted door time - last_changed can be from recovery, not real cycle start.
        try:
            if self.start_time and not self.last_door_closed_trusted and last_changed:
                last_changed_dt = _parse_utc(str(last_changed))
                if last_changed_dt and self.start_time < last_changed_dt:
                    # An HA restart wipes and re-creates this entity, so right after a restart
                    # last_changed is merely the moment the entity was re-published - NOT when
                    # the cycle went Running. The "cycle 2" correction below took that wipe
                    # artifact at face value and dragged a real 11:41 start to 13:24. Any
                    # restore anchor within a few minutes of the app's own init time is the
                    # wipe, not information - keep the restored start_time instead.
                    wipe_grace_s = getattr(self, "restore_wipe_anchor_grace_s", 300)
                    anchor_age_s = (self._now_utc() - last_changed_dt).total_seconds()
                    if abs(anchor_age_s) <= wipe_grace_s:
                        self.log(
                            f"Restore: entity last_changed {self._strftime_local(last_changed_dt)} is within "
                            f"{wipe_grace_s}s of app init - restart artifact, keeping start_time "
                            f"{self._strftime_local(self.start_time)}",
                            level="INFO",
                        )
                    else:
                        gap_seconds = (last_changed_dt - self.start_time).total_seconds()
                        # entity_last_changed is the weakest provenance we have (rank 6). The
                        # wipe grace above catches the restart artifact; this rank gate covers
                        # the rest - it must never overwrite a clock a stronger source already
                        # vouched for, above all one restored from the durable store (rank 3),
                        # which outlives the entity entirely.
                        if gap_seconds >= self.pause_window_minutes * 60 and self._start_time_rank() >= 6:
                            self.log(
                                f"Restore: correcting start_time to entity last_changed (cycle 2) "
                                f"(was {self._strftime_local(self.start_time)}, now {self._strftime_local(last_changed_dt)})",
                                level="INFO",
                            )
                            self.start_time = last_changed_dt
                            self._start_time_source = "entity_last_changed"
                            self._push_corrected_start_time_to_entity()
        except (TypeError, ValueError, AttributeError):
            pass

        # Restore: validate start_time against power history; if there was a long off-period after it, re-infer real cycle start
        if self.start_time and self.power_sensor:
            try:
                end_time = self._now_utc()
                from_time = self.start_time - timedelta(minutes=5)
                inferred = self._infer_cycle_start_from_power_history(from_time, end_time)
                if inferred is not None:
                    gap_seconds = (inferred - self.start_time).total_seconds()
                    if gap_seconds >= self.restore_start_gap_minutes * 60:
                        self.log(
                            f"Restore: corrected start_time from power history "
                            f"(was {self._strftime_local(self.start_time)}, now {self._strftime_local(inferred)})",
                            level="INFO",
                        )
                        self.start_time = inferred
                        self._start_time_source = "power_history"
                        self._push_corrected_start_time_to_entity()
                        if self.use_energy_detection:
                            self._restore_energy_state_from_history()
            except Exception as e:
                self.log(f"Could not validate/correct start_time from power history: {e}", level="DEBUG")

        try:
            energy_at_start = attrs.get("energy_at_start")
            if energy_at_start is not None:
                self.energy_start = float(energy_at_start)
                self.log(f"Restored energy_at_start: {self.energy_start}", level="DEBUG")
        except (TypeError, ValueError, AttributeError):
            pass

        # Restore who started this cycle (see _begin_running_cycle / _attribute): an AppDaemon
        # reload mid-Running never re-runs _begin_running_cycle, so without this self._cycle_actor
        # would fall back to unknown even though we captured it before the restart.
        try:
            started_by = attrs.get("started_by")
            started_by_method = attrs.get("started_by_method")
            self._cycle_actor = self._cycle_actor_from_state_attrs(
                {"started_by": started_by, "started_by_method": started_by_method}
            )
            if self._cycle_actor.get("person"):
                self.log(f"Restored started_by: {self._cycle_actor['person']}", level="DEBUG")
        except (TypeError, ValueError, AttributeError):
            pass

        try:
            cost_attr = attrs.get("session_cost_kr")
            if cost_attr is not None:
                self._session_cost_kr = float(cost_attr)
                self.log(f"Restored session_cost_kr: {self._session_cost_kr}", level="DEBUG")
        except (TypeError, ValueError, AttributeError):
            pass

        try:
            prog = attrs.get("detected_programme")
            if prog and prog != "unknown":
                restored_temp = attrs.get("detected_temperature")
                if restored_temp and restored_temp not in ("unknown", "unavailable", ""):
                    self.detected_temperature = restored_temp
                self.detected_programme = prog
                profile = self._get_profile(prog, self.detected_temperature)
                if profile.get("heats") is True:
                    self.observed_heating = True
                self.log(f"Restored detected programme: {prog} {self.detected_temperature or ''}", level="DEBUG")
        except (TypeError, AttributeError):
            pass

        # Restore heating from power history when entity has heating_bursts=0 (e.g. false finish, restart).
        # Graph shows 2000W+ at 14:02 but we may have missed the callback - history has the truth.
        try:
            hb = attrs.get("heating_bursts")
            if (hb is None or int(hb or 0) == 0) and not self.observed_heating:
                self._restore_heating_from_power_history()
        except (TypeError, ValueError, AttributeError):
            pass

        # Restore expected_dur_at_start from selector only when user had confirmed (entity attribute).
        # Otherwise the selector may hold an auto-filled prediction from a previous run.
        try:
            confirmed_attr = attrs.get("programme_confirmed_by_user")
            user_confirmed = confirmed_attr is True or confirmed_attr == "true" or confirmed_attr == "True"
        except Exception:
            user_confirmed = False
        if user_confirmed and self.confirm_entity:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
                    if prog and prog != "unknown":
                        dur = self._get_programme_duration(prog, temp, use_learned=False)
                        if dur:
                            self.expected_dur_at_start = dur
                            self._guard_bar_class = (prog, temp)
                            self.log(f"Restored expected_dur_at_start from selector: {dur:.0f} min", level="DEBUG")
            except Exception:
                pass
        if self.expected_dur_at_start is None:
            try:
                v = attrs.get("expected_dur_at_start")
                if v not in (None, "", "unknown", "unavailable"):
                    self.expected_dur_at_start = float(v)
                    # Bar programme key persisted alongside (see the Running publish) so the
                    # energy-disproof lowering keeps working across the frequent mid-cycle
                    # app restarts; stays None on older entities (lowering disabled until
                    # the live classification re-keys it).
                    self._guard_bar_class = self._parse_guard_bar_key(attrs.get("expected_dur_key"))
                    self.log(f"Restored expected_dur_at_start from entity: {self.expected_dur_at_start:.0f} min", level="DEBUG")
            except (TypeError, ValueError, AttributeError):
                pass

        # Restore delayed-start-trimmed flag before the door-close clamp below (and the live one
        # in _check_energy_finish) so a start_time we already slid past door-close is not dragged
        # back to it.
        try:
            trimmed_attr = attrs.get("delayed_start_trimmed")
            self._delayed_start_trimmed = self._attr_bool_true(trimmed_attr)
        except (TypeError, ValueError, AttributeError):
            pass

        # Correct start_time if it's after last_door_closed_at (e.g. wrong from bad recovery).
        if (self.start_time and self.last_door_closed_trusted and self.last_door_closed_at and
                self.start_time > self.last_door_closed_at and not self._delayed_start_trimmed):
            gap = (self.start_time - self.last_door_closed_at).total_seconds()
            if gap >= 60:
                self.log(
                    f"Restore: correcting start_time (was after door close) "
                    f"{self._strftime_local(self.start_time)} -> {self._strftime_local(self.last_door_closed_at)}",
                    level="INFO",
                )
                self.start_time = self.last_door_closed_at
                self._push_corrected_start_time_to_entity()

        if self.confirm_entity:
            try:
                # Restore from the persisted state entity attribute, not from the dropdown.
                # The dropdown can be set by auto-detection (via select_option), so reading
                # it here would incorrectly mark auto-detected programmes as user-confirmed.
                confirmed_attr = attrs.get("programme_confirmed_by_user")
                if confirmed_attr is True or confirmed_attr == "true" or confirmed_attr == "True":
                    self.programme_confirmed_by_user = True
                    by_attr = attrs.get("programme_confirmed_by") or ""
                    self.confirmed_by_username = by_attr if by_attr else None
                    label = self.get_state(self.confirm_entity)
                    by_str = f" by {self.confirmed_by_username}" if self.confirmed_by_username else ""
                    self.log(f"Restored user-confirmed programme: {label}{by_str}", level="INFO")
            except Exception:
                pass

        if self.start_time and self.use_energy_detection:
            # Prefer loading energy buffer and last_high_energy_at from HA history so we
            # don't lose the stable clock after a restart (no extra 15 min wait).
            if not self._restore_energy_state_from_history():
                self._start_energy_detection()
        if self.start_time:
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = self.run_in(
                self._running_watchdog_timeout,
                int(self.max_running_hours * 3600),
            )
        # Restart poll timer so low-power detection via polling works after an app reload
        # (listen_state won't fire if the power sensor value hasn't changed since restart).
        if not self.poll_timer:
            poll_interval = int(self.args.get("poll_interval_s", 60))
            self.poll_timer = self.run_in(self._poll_power, poll_interval)
        # Periodic power-history check: catch heating we may have missed (Shelly may not push every reading).
        if not self.history_poll_timer:
            interval = int(self.args.get("history_check_interval_s", 300))
            self.history_poll_timer = self.run_in(self._periodic_check_power_history, interval)
        # Restore last_high_energy_at from state entity if we didn't get it from history.
        try:
            last_high_str = attrs.get("last_high_energy_at")
            if last_high_str and self.last_high_energy_at is None:
                self.last_high_energy_at = _parse_utc(last_high_str)
                if self.last_high_energy_at:
                    self.log(f"Restored last_high_energy_at from state: {last_high_str}", level="DEBUG")
        except (TypeError, ValueError, AttributeError):
            pass

        # cycle_start_time may be empty after a bad Off transition or HA restart; power is active - re-infer.
        try:
            boot_watts = float(self.get_state(self.power_sensor) or 0)
        except (ValueError, TypeError):
            boot_watts = 0.0
        if boot_watts >= self.start_w and not self.start_time:
            self._infer_wash_start_time_when_missing()
        # programme_confirmed_by_user can be missing on the sensor while input_select still shows Bomuld/ECO.
        if boot_watts >= self.start_w and self.start_time and self.confirm_entity and not self.programme_confirmed_by_user:
            self._recovery_sync_programme_from_selector()

    def _infer_wash_start_time_when_missing(self):
        """Set start_time from recorder when attributes were lost but the wash is clearly running."""
        inferred = self._infer_start_from_state_history()
        if inferred is None:
            inferred = self._infer_first_sustained_high_power_start(hours=8)
        if inferred is None:
            self.log(
                "Recovery: could not infer cycle start (no state transition and no sustained high power in history)",
                level="WARNING",
            )
            return
        self.start_time = inferred
        self.log(
            f"Recovery: inferred missing cycle start as {self._strftime_local(inferred)}",
            level="INFO",
        )
        self._push_corrected_start_time_to_entity()

    def _infer_first_sustained_high_power_start(self, hours: float = 8):
        """First time in the last `hours` h where power reached start_w twice within 25 minutes (wash underway)."""
        if not self.power_sensor:
            return None
        try:
            end_time = self._now_utc()
            start_win = end_time - timedelta(hours=hours)
            hist = self.get_history(
                entity_id=self.power_sensor,
                start_time=start_win,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.power_sensor)
            points = whist.parse_power_points(hist)
            if len(points) < 2:
                return None
            points.sort(key=lambda x: x[0])
            return wpow.find_first_sustained_high(
                points, self.start_w, window_seconds=25 * 60, needed=2
            )
        except Exception as e:
            self.log(f"Could not infer start from power history: {e}", level="DEBUG")
            return None

    def _recovery_sync_programme_from_selector(self):
        """If the sensor lost programme_confirmed_by_user but the user left a real programme selected, restore the flag."""
        if not self.confirm_entity:
            return
        try:
            label = self.get_state(self.confirm_entity)
            if not label or label in ("Auto (unconfirmed)", "unknown", "unavailable"):
                return
            prog = self._LABEL_TO_KEY.get(label, "unknown")
            if not prog or prog == "unknown":
                return
            self.programme_confirmed_by_user = True
            temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
            dur = self._get_programme_duration(prog, temp, use_learned=False)
            if dur:
                self.expected_dur_at_start = float(dur)
                self._guard_bar_class = (prog, temp)
            self.log(
                f"Recovery: selector shows '{label}' with active wash but programme_confirmed was false "
                f"— restoring confirmation for ETA/guards",
                level="INFO",
            )
            full = self.get_state(self.state_entity, attribute="all") or {}
            attrs = dict((full.get("attributes") or {}))
            attrs["programme_confirmed_by_user"] = True
            attrs["programme_confirmed_by"] = self.confirmed_by_username or ""
            attrs["expected_dur_at_start"] = self.expected_dur_at_start if self.expected_dur_at_start is not None else ""
            attrs["expected_dur_key"] = self._guard_bar_key_str()
            self._set_state_entity( state="Running", attributes=attrs, replace=True)
        except Exception as e:
            self.log(f"Recovery sync programme from selector failed: {e}", level="DEBUG")

    def _flatten_history(self, hist, entity_id=None):
        """AppDaemon get_history returns list[list[dict]] (or occasionally dict). Normalize to list[dict]."""
        return whist.flatten_history(hist, entity_id)

    def _infer_start_from_state_history(self):
        """Infer cycle start from state entity history: when did we transition to Running from Off/Emptied?
        Skips Unemptied->Running (recovery) - we want the original cycle start.
        Used when last_door_closed_at is missing (e.g. lost during bad recovery)."""
        if not self.state_entity:
            return None
        try:
            end_time = self._now_utc()
            start_time = end_time - timedelta(hours=6)
            hist = self.get_history(
                entity_id=self.state_entity,
                start_time=start_time,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.state_entity)
            if len(hist) < 2:
                return None
            # Iterate chronologically; keep the most recent Off/Emptied -> Running transition.
            prev_state = None
            result = None
            for entry in hist:
                state = entry.get("state")
                ts_str = entry.get("last_changed") or entry.get("last_updated")
                if not ts_str:
                    continue
                t = _parse_utc(ts_str)
                if t is None:
                    continue
                if state == "Running" and prev_state and prev_state not in ("Running", "Unemptied", "Paused"):
                    result = t
                prev_state = state
            return result
        except Exception as e:
            self.log(f"Could not infer start from state history: {e}", level="DEBUG")
            return None

    def _infer_cycle_start_from_power_history(self, from_time, to_time):
        """If there was a long low-power gap after from_time, return the first sustained high-power time after that gap.

        Used on restore to correct a stale cycle_start_time (e.g. machine was off 10:00–13:40, real start 13:40).
        Returns datetime or None. Requires continuous low power >= restore_start_gap_minutes after from_time, then
        at least 2 consecutive readings >= start_w to count as sustained high power.
        """
        if not self.power_sensor or not self.start_time:
            return None
        try:
            hist = self.get_history(
                entity_id=self.power_sensor,
                start_time=from_time,
                end_time=to_time,
            )
            hist = self._flatten_history(hist, self.power_sensor)
            if len(hist) < 3:
                return None
            points = whist.parse_power_points(hist)
            if len(points) < 3:
                return None
            points.sort(key=lambda x: x[0])
            return wpow.find_start_after_gap(
                points, from_time, to_time, self.start_w,
                gap_min_seconds=self.restore_start_gap_minutes * 60,
            )
        except Exception as e:
            self.log(f"Could not infer cycle start from power history: {e}", level="DEBUG")
            return None

    def _restore_heating_from_power_history(self) -> bool:
        """Infer observed_heating and heating_phase_count from power history when entity has 0.
        The graph shows 2000W+ at 14:02 but we may have missed it (restart, recovery, callback gap).
        Returns True if we found heating in history."""
        if not self.start_time or not self.power_sensor:
            return False
        if self.observed_heating and self.heating_phase_count > 0:
            return True
        try:
            end_time = self._now_utc()
            hist = self.get_history(
                entity_id=self.power_sensor,
                start_time=self.start_time,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.power_sensor)
            if len(hist) < 2:
                return False
            points = whist.parse_power_points(hist)
            if len(points) < 2:
                return False
            points.sort(key=lambda x: x[0])
            bursts, max_w = wpow.count_heating_bursts(points, self.max_power_seen)
            if bursts > 0 or max_w > wpow.BURST_ON_WATTS:
                self.observed_heating = True
                self.heating_phase_count = max(self.heating_phase_count, bursts)
                self.max_power_seen = max(self.max_power_seen, max_w)
                self.log(
                    f"Restored heating from power history: {bursts} bursts, max {max_w:.0f}W "
                    f"(entity had heating_bursts=0)",
                    level="INFO",
                )
                return True
        except Exception as e:
            self.log(f"Could not restore heating from power history: {e}", level="DEBUG")
        return False

    def _backfill_heating_from_history_for_feedback(self) -> tuple[int | None, float | None]:
        """Compute heating_bursts and max_power_w from power history for feedback when live counters are implausible.
        Returns (bursts, max_w) or (None, None) if history unavailable. Does not modify instance state."""
        if not self.start_time or not self.power_sensor:
            return (None, None)
        try:
            end_time = self._now_utc()
            hist = self.get_history(
                entity_id=self.power_sensor,
                start_time=self.start_time,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.power_sensor)
            if len(hist) < 2:
                return (None, None)
            points = whist.parse_power_points(hist)
            if len(points) < 2:
                return (None, None)
            points.sort(key=lambda x: x[0])
            return wpow.count_heating_bursts(points, 0.0)
        except Exception as e:
            self.log(f"Could not backfill heating from history for feedback: {e}", level="DEBUG")
            return (None, None)

    def _periodic_check_power_history(self, kwargs):
        """During Running/Paused, periodically fetch power history to catch heating we may have missed.
        Shelly may not push every reading; listen_state + 30s poll can miss brief spikes.
        The recorder has the full history - we proactively read it every 5 min.
        Once we've detected heating, we stop - no need to keep confirming."""
        current_state = self.get_state(self.state_entity)
        if current_state not in ("Running", "Paused"):
            self._safe_cancel_timer(self.history_poll_timer)
            self.history_poll_timer = None
            return
        if self.observed_heating and self.heating_phase_count > 0:
            self._safe_cancel_timer(self.history_poll_timer)
            self.history_poll_timer = None
            return
        self._restore_heating_from_power_history()
        if self.observed_heating and self.heating_phase_count > 0:
            self._safe_cancel_timer(self.history_poll_timer)
            self.history_poll_timer = None
            return
        interval = int(self.args.get("history_check_interval_s", 300))
        self.history_poll_timer = self.run_in(self._periodic_check_power_history, interval)

    def _restore_energy_state_from_history(self) -> bool:
        """Load energy buffer and last_high_energy_at from HA history. Returns True if usable."""
        if not self.start_time or not self.energy_sensor:
            return False
        try:
            end_time = self._now_utc()
            hist = self.get_history(
                entity_id=self.energy_sensor,
                start_time=self.start_time,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.energy_sensor)
            if len(hist) < 2:
                self.log("Not enough energy history to restore buffer", level="DEBUG")
                return False
            points = whist.parse_power_points(hist)
            if len(points) < 2:
                return False
            points.sort(key=lambda x: x[0])
            cutoff = end_time - timedelta(minutes=20)
            self.energy_buffer = [(t, e) for t, e in points if t >= cutoff]
            self.last_energy_value = points[-1][1]
            self.last_energy_time = points[-1][0]
            # Last time we saw implied watts above threshold (cycle was still consuming)
            high_ends = wpow.high_power_end_times(points, self.energy_active_watts)
            self.last_high_energy_at = high_ends[-1] if high_ends else None
            if self.last_high_energy_at is None:
                self.last_high_energy_at = self.start_time
            self.energy_stable_start_time = None
            self.finish_confirmed = False
            self.energy_check_timer = self.run_in(self._check_energy_finish, self.energy_check_interval)
            self.log(
                f"Restored energy state from HA history: {len(self.energy_buffer)} points, "
                f"last_high_energy_at={self._strftime_local(self.last_high_energy_at, '%H:%M') if self.last_high_energy_at else None}",
                level="INFO",
            )
            return True
        except Exception as e:
            self.log(f"Could not restore energy state from history: {e}", level="DEBUG")
            return False

    def _estimate_cycle_end_from_history(self, expected_duration_min: float | None = None):
        """Estimate when the cycle actually ended from HA energy history.

        Returns a datetime (cycle end) or None. Used when transitioning to Unemptied
        so we record the true duration even if detection was delayed (e.g. after restart).
        When expected_duration_min is set (e.g. from user-confirmed programme), we pick
        the end-of-high-power moment whose duration from start is closest to that value,
        so we get a more accurate finish time when the user opened the door some time
        after the programme actually ended.
        """
        if not self.start_time or not self.energy_sensor:
            return None
        try:
            end_time = self._now_utc()
            hist = self.get_history(
                entity_id=self.energy_sensor,
                start_time=self.start_time,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.energy_sensor)
            if len(hist) < 2:
                self.log("Cycle end from history: not enough history points", level="DEBUG")
                return None
            points = whist.parse_power_points(hist)
            if len(points) < 2:
                self.log("Cycle end from history: not enough valid energy points", level="DEBUG")
                return None
            points.sort(key=lambda x: x[0])
            run_minutes_max = (end_time - self.start_time).total_seconds() / 60

            # Collect all timestamps that are "end of a high-power period" (implied watts above threshold).
            high_end_candidates = wpow.high_power_end_times(points, self.energy_active_watts)

            if not high_end_candidates:
                self.log("Cycle end from history: no high-power end candidates in energy series", level="DEBUG")
                return None

            if expected_duration_min is not None and expected_duration_min > 0:
                # User-confirmed (or hinted) programme: pick the high-end that gives duration
                # closest to expected, within valid range. This avoids picking a late spike or
                # early heating end when the real cycle end is around expected_duration.
                best_end = wpow.end_closest_to_expected(
                    high_end_candidates, self.start_time, expected_duration_min,
                    self.min_cycle_minutes, run_minutes_max,
                )
                if best_end is not None:
                    for t, _ in points:
                        if t > best_end:
                            return best_end + (t - best_end) / 2
                    return best_end + timedelta(minutes=2)
                # Fall through to last-high-end if no candidate in range

            # No hint or no candidate in range: use last high-power end (existing behaviour).
            last_high_end = high_end_candidates[-1]
            for t, _ in points:
                if t > last_high_end:
                    return last_high_end + (t - last_high_end) / 2
            return last_high_end + timedelta(minutes=2)
        except Exception as e:
            self.log(f"Could not estimate cycle end from history: {e}", level="DEBUG")
            return None

    def _detect_post_cycle_slow_spin_pattern(self) -> bool:
        """Detect the distinct post-cycle slow-spin pattern from power history.

        After the programme ends the motor often keeps turning slowly, producing
        regular low-amplitude oscillations (sawtooth/ripple) in power, unlike
        true idle (flat) or mid-cycle soak. We fetch recent power readings and
        check for: mean in 10-70W and elevated std (ripple) vs flat idle.
        """
        if not self.power_sensor:
            return False
        try:
            end_time = self._now_utc()
            start_time = end_time - timedelta(minutes=self.post_cycle_pattern_window_minutes)
            hist = self.get_history(
                entity_id=self.power_sensor,
                start_time=start_time,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.power_sensor)
            if len(hist) < 6:
                return False
            points = whist.parse_power_points(hist)
            if len(points) < 6:
                return False
            points.sort(key=lambda x: x[0])
            watts = [w for _, w in points]
            mean_w, std_w = wpow.mean_and_std(watts)
            if wpow.slow_spin_pattern_ok(
                mean_w, std_w,
                self.post_cycle_pattern_mean_low,
                self.post_cycle_pattern_mean_high,
                self.post_cycle_pattern_min_std,
            ):
                self.log(
                    f"Post-cycle slow-spin pattern detected (power mean={mean_w:.1f}W std={std_w:.1f}W over {len(points)} points)",
                    level="DEBUG",
                )
                return True
            return False
        except Exception as e:
            self.log(f"Could not detect post-cycle pattern: {e}", level="DEBUG")
            return False

    def _get_current_power(self):
        """Get current power reading in watts."""
        try:
            power_state = self.get_state(self.power_sensor)
            if power_state not in ["unknown", "unavailable", None]:
                return float(power_state)
        except (ValueError, TypeError):
            pass
        return 0

    def _get_recent_power_history(self, window_minutes: float):
        """Fetch raw power history for the last window_minutes. Returns list of (datetime_utc, watts)."""
        if not self.power_sensor or window_minutes <= 0:
            return []
        try:
            end_time = self._now_utc()
            start_time = end_time - timedelta(minutes=window_minutes)
            hist = self.get_history(
                entity_id=self.power_sensor,
                start_time=start_time,
                end_time=end_time,
            )
            hist = self._flatten_history(hist, self.power_sensor)
            points = whist.parse_power_points(hist)
            points.sort(key=lambda x: x[0])
            return points
        except Exception as e:
            self.log(f"Could not get recent power history: {e}", level="DEBUG")
            return []

    def _get_tail_stats_time_weighted(self, window_minutes: float):
        """Time-weighted mean, std, peak, duty_above for tail window. Event-driven sensors have many
        points during low-power ripple and few during high-power; time-weighting avoids bias.
        Returns (mean_w, std_w, peak_w, duty_above_active) or (None, None, None, None) if insufficient data."""
        points = self._get_recent_power_history(window_minutes)
        if len(points) < 5:
            return (None, None, None, None)
        return wpow.time_weighted_stats(points, self._now_utc(), self.energy_active_watts)

    def _tail_pulse_reset_threshold_watts(self) -> float:
        """Threshold for resetting last_tail_pulse_at. In FinishingTail use finishing_tail_pulse_reset_watts
        so Miele anti-crease nudges (10–55W) do not block the tail-pulse timeout; outside tail keep 10W."""
        if self.in_finishing_tail:
            return self.finishing_tail_pulse_reset_watts
        return self.tail_pulse_threshold_watts

    def _get_last_tail_pulse_time(self):
        """Return the time of the most recent power history point above tail reset threshold, or None."""
        thr = self._tail_pulse_reset_threshold_watts()
        points = self._get_recent_power_history(2.0)  # last 2 min
        return wpow.last_time_above(points, thr)

    def _refresh_tail_pulse_tracking(self):
        """While in FinishingTail, merge last_tail_pulse_at with recorder history (missed live callbacks)
        and current power so tail-pulse timeout reflects the true last pulse."""
        if not self.in_finishing_tail:
            return
        now = self._now_utc()
        hist_last = self._get_last_tail_pulse_time()
        if hist_last and (self.last_tail_pulse_at is None or hist_last > self.last_tail_pulse_at):
            self.last_tail_pulse_at = hist_last
        try:
            pw = self._get_current_power()
            if pw is not None and pw > self._tail_pulse_reset_threshold_watts():
                self.last_tail_pulse_at = now
        except (TypeError, ValueError):
            pass

    def _tail_pulse_timeout_met(self) -> bool:
        """True when we're in FinishingTail, current power is low (≤ finish_standby_max_watts), and no tail pulse
        has occurred for at least tail_pulse_timeout_seconds. Data: 55s had 0 early triggers on long heated cycles."""
        if not self.in_finishing_tail or self.last_tail_pulse_at is None:
            return False
        current_power = self._get_current_power()
        if current_power is None or current_power > self.finish_standby_max_watts:
            return False
        gap = (self._now_utc() - self.last_tail_pulse_at).total_seconds()
        return gap >= self.tail_pulse_timeout_seconds

    def _tail_idle_window_ok(self) -> bool:
        """Require a short recent window to be truly quiet before declaring finished.
        Prevents false finish during spin/anti-crease where pulses are below pulse-reset threshold."""
        lookback_min = max(1.0, self.tail_idle_confirm_seconds / 60.0)
        points = self._get_recent_power_history(lookback_min)
        if len(points) < 3:
            return False
        cutoff = self._now_utc() - timedelta(seconds=self.tail_idle_confirm_seconds)
        return wpow.tail_idle_ok(
            points, cutoff, self.tail_idle_peak_max_watts, self.post_cycle_idle_watts
        )

    def _extract_tail_pulse_times(self, points):
        """Extract pulse timestamps from power points using an edge detector with gap de-duplication."""
        return wpow.extract_pulse_times(
            points, self.tail_pattern_pulse_threshold_watts, self.tail_pattern_min_gap_seconds
        )

    def _update_tail_pattern_lock(self):
        """Lock to repeatable anti-crease/spin pulse cadence while in FinishingTail."""
        if not self.in_finishing_tail:
            self.tail_pattern_locked = False
            self.tail_pattern_cycle_seconds = None
            self.tail_pattern_last_pulse_at = None
            self.tail_pattern_locked_at = None
            return
        points = self._get_recent_power_history(self.tail_pattern_lock_window_minutes)
        pulse_times = self._extract_tail_pulse_times(points)
        if pulse_times:
            self.tail_pattern_last_pulse_at = pulse_times[-1]
        med_gap = wpow.pulse_cadence(
            pulse_times,
            self.tail_pattern_min_gap_seconds,
            self.tail_pattern_max_gap_seconds,
            self.tail_pattern_lock_min_pulses,
            self.tail_pattern_max_jitter_fraction,
        )
        if med_gap is None:
            return
        newly_locked = not self.tail_pattern_locked
        self.tail_pattern_locked = True
        self.tail_pattern_cycle_seconds = med_gap
        self.tail_pattern_locked_at = self.tail_pattern_locked_at or self._now_utc()
        if newly_locked:
            self.log(
                f"[TAIL] Tail cadence locked (cycle ~{med_gap:.1f}s, pulses={len(pulse_times)}, thr={self.tail_pattern_pulse_threshold_watts:.0f}W)",
                level="INFO",
            )

    def _tail_pattern_break_met(self) -> bool:
        """True when we had a locked tail cadence and enough expected pulses are now missing."""
        if not self.in_finishing_tail:
            return False
        if not self.tail_pattern_locked or not self.tail_pattern_cycle_seconds or not self.tail_pattern_last_pulse_at:
            return False
        current_power = self._get_current_power()
        if current_power is None or current_power > self.finish_standby_max_watts:
            return False
        required_gap = (
            self.tail_pattern_cycle_seconds * self.tail_pattern_break_missed_pulses
            + self.tail_pattern_break_confirm_seconds
        )
        gap = (self._now_utc() - self.tail_pattern_last_pulse_at).total_seconds()
        return gap >= required_gap

    def _try_finish_via_standby(self, run_min: float, guard_dur: float, tick_prog, tick_temp, tick_class) -> bool:
        """If we are in FinishingTail and the next tail pulse has not arrived within timeout (power low ≥55s), transition to Unemptied (announce)."""
        if not self.in_finishing_tail:
            return False
        pulse_timeout = self._tail_pulse_timeout_met()
        pattern_break = self._tail_pattern_break_met()
        if not pulse_timeout and not pattern_break:
            return False
        if not self._tail_idle_window_ok():
            return False
        if not self._meets_finish_time_guards(run_min, guard_dur or 0):
            return False
        if not self._is_valid_completed_cycle():
            return False
        self._pending_end_reason = "tail_pattern_break" if pattern_break else "tail_to_standby"
        self.in_finishing_tail = False
        self.in_finishing_tail_entered_at = None
        self.last_tail_pulse_at = None
        self.tail_pattern_locked = False
        self.tail_pattern_cycle_seconds = None
        self.tail_pattern_last_pulse_at = None
        self.tail_pattern_locked_at = None
        if pattern_break:
            self.log(
                "[TAIL] Tail cadence break (missing expected pulses after lock, power ≤{}W) - transitioning to Unemptied (announce)".format(
                    self.finish_standby_max_watts
                ),
                level="INFO",
            )
        else:
            self.log(
                "[TAIL] Tail pulse timeout (no pulse >{}W for {:.0f}s, power ≤{}W) - transitioning to Unemptied (announce)".format(
                    self.finishing_tail_pulse_reset_watts, self.tail_pulse_timeout_seconds, self.finish_standby_max_watts
                ),
                level="INFO",
            )
        self._transition_to_unemptied()
        return True

    def _is_post_end_tail_window(self, run_min: float, expected_dur: float, programme: str) -> bool:
        """True when run time is within anti_crease_near_end_minutes of expected end, or past it; or when programme unknown, past anti_crease_min_runtime_minutes."""
        if programme and programme != "unknown":
            # Near or past expected end
            if expected_dur and run_min >= expected_dur - self.anti_crease_near_end_minutes:
                return True
            if expected_dur and run_min >= expected_dur:
                return True
            return False
        # Programme unknown: allow after minimum runtime so we can still finish via tail pattern
        return run_min >= self.anti_crease_min_runtime_minutes

    def _recent_true_activity_block(self, window_minutes: float | None = None) -> bool:
        """True if there was recent heating or sustained high power in the window (disqualifies anti-crease finish).
        Uses time-weighted duty so event-heavy low-power ripple does not dominate."""
        w = window_minutes or self.anti_crease_window_minutes
        mean_w, std_w, peak_w, duty_above = self._get_tail_stats_time_weighted(w)
        if mean_w is None:
            points = self._get_recent_power_history(w)
            return wpow.activity_from_point_counts(
                points, self.energy_active_watts, self.anti_crease_max_duty_above_active
            )
        if duty_above > self.anti_crease_max_duty_above_active:
            return True
        if peak_w > wpow.HEATING_BURST_WATTS:
            return True
        return False

    def _detect_anti_crease_pattern(self, time_weighted: bool = True):
        """Detect post-end anti-crease tail (low baseline + short periodic bumps). When time_weighted
        is True (default), stats are time-weighted to avoid event-count bias from chatty low-power ripple.
        Returns (ok: bool, tail_mean_w, tail_std_w, tail_peak_w)."""
        if time_weighted:
            mean_w, std_w, peak_w, duty_above = self._get_tail_stats_time_weighted(self.anti_crease_window_minutes)
            if mean_w is None:
                return (False, None, None, None)
            ok = wpow.anti_crease_ok_from_stats(
                mean_w, std_w, peak_w, duty_above,
                self.anti_crease_tail_max_mean_w,
                self.anti_crease_tail_min_std_w,
                self.anti_crease_max_duty_above_active,
                self.anti_crease_tail_max_peak_w,
            )
            return (ok, mean_w, std_w, peak_w)
        points = self._get_recent_power_history(self.anti_crease_window_minutes)
        return wpow.anti_crease_from_points(
            points,
            self.energy_active_watts,
            self.anti_crease_tail_max_mean_w,
            self.anti_crease_tail_min_std_w,
            self.anti_crease_max_duty_above_active,
            self.anti_crease_tail_max_peak_w,
        )

    def _power_looks_like_cycle_end(self, window_minutes: float | None = None) -> tuple[bool, float | None, float | None]:
        """True only if recent power pattern looks like real cycle end (anti-crease or machine off), not mid-cycle rinse.
        Mid-cycle rinse: mean ~50–80W, peaks 150–250W. Real end: mean ~18–45W, peak <120W, or flat idle (mean <12W).
        Returns (ok, mean_w, peak_w) for logging."""
        w = window_minutes or self.anti_crease_window_minutes
        points = self._get_recent_power_history(w)
        return wpow.looks_like_cycle_end(
            points,
            self.finish_power_gate_max_mean_w,
            self.finish_power_gate_max_peak_w,
            self.finish_power_gate_off_max_mean_w,
            self.finish_power_gate_off_max_peak_w,
        )

    def _get_run_duration_minutes(self):
        """Get how long the current cycle has been running in minutes."""
        if self.start_time is None:
            return 0
        return (self._now_utc() - self.start_time).total_seconds() / 60

    def _get_energy_used(self):
        """Get energy consumed since cycle start in kWh."""
        if self.energy_start is None:
            return 0
        try:
            current_energy = self.get_state(self.energy_sensor)
            if current_energy is not None and current_energy not in ["unknown", "unavailable"]:
                return float(current_energy) - self.energy_start
        except (ValueError, TypeError):
            pass
        return 0

    def _is_valid_completed_cycle(self):
        """Check if the cycle ran long enough and used enough energy."""
        run_minutes = self._get_run_duration_minutes()
        energy_used = self._get_energy_used()

        is_valid = (run_minutes >= self.min_cycle_minutes and
                    energy_used >= self.min_energy_kwh)

        self.log(f"Cycle validation: {run_minutes:.1f} min (need {self.min_cycle_minutes}), "
                 f"{energy_used:.3f} kWh (need {self.min_energy_kwh}) -> {'valid' if is_valid else 'invalid'}",
                 level="DEBUG")

        return is_valid

    def _classify_cycle_completion(
        self,
        run_minutes: float,
        energy_kwh: float,
        heating_bursts: int,
        max_power_w: float,
        predicted: str,
        predicted_temperature,
        confirmed: str,
        confirmed_temperature,
        transition_path: str,  # user_cycle_end | anti_crease_pattern | low_power_detected | door_opened_first
        spin_rpm=None,
        user_confirmed_override: bool | None = None,  # For migration: use rec's programme_user_confirmed
    ):
        """Classify a completed cycle for learning quality. Returns completion_class, valid_for_learning, validation_flags, end_reason.
        Finish detection decides UI state; validation only classifies the saved record.

        heating_bursts / max_power_w / predicted / predicted_temperature / spin_rpm are
        accepted but not consulted - kept in the signature because every caller passes
        them by keyword and the record shape they belong to is written elsewhere.
        """
        user_conf = user_confirmed_override if user_confirmed_override is not None else self.programme_confirmed_by_user
        return wcls.classify_cycle_completion(
            run_minutes=run_minutes,
            energy_kwh=energy_kwh,
            confirmed=confirmed,
            transition_path=transition_path,
            profile=self._get_profile(confirmed, confirmed_temperature),
            user_conf=user_conf,
            guard_fraction=(self.completion_guard_fraction_user_confirmed if user_conf else self.completion_guard_fraction),
            min_cycle_minutes=self.min_cycle_minutes,
            min_energy_kwh=self.min_energy_kwh,
            validation_key=wp.learn_key_for(self.PROGRAMME_PROFILES, confirmed, confirmed_temperature),
        )

    def _should_change_state(self, new_state, force=False):
        """Check if we should allow a state change.
        
        Args:
            new_state: Target state
            force: If True, bypass cooling period (for authoritative events like door open, or Off/Emptied -> Running so second cycle gets correct start time)
        """
        if self.get_state(self.state_entity) == new_state:
            return False
        now = self._now_utc()
        if not force and self.last_state_change and (now - self.last_state_change).total_seconds() < self.cooling_period:
            self.log(f"In cooling period", level="DEBUG")
            return False
        self.last_state_change = now
        return True

    def _record_power_reading(self, watts):
        """Record power readings to detect patterns"""
        self.power_readings.append(watts)
        if len(self.power_readings) > self.pattern_window:
            self.power_readings.pop(0)

    def _door_previous_state_unreliable(self, old) -> bool:
        """True if `old` is a HA restart / entity-restore state, not a real prior door position.

        After HA (or Zigbee) restarts, history often shows: closed -> unknown -> closed with no one touching
        the door. Treating that second 'closed' as last_door_closed_at shifts cycle_start and ETA wrongly.
        We still process transitions from a known open/closed `old` (including the first callback where
        old may be None - AppDaemon typically does not fire until a real change).
        """
        if old is None:
            return False
        s = str(old).strip().lower()
        return s in ("unknown", "unavailable", "none")

    def _door_is_physically_open(self) -> bool:
        """True if the door sensor currently reports open (respects door_sensor_inverted)."""
        door_state = self.get_state(self.door_sensor)
        if door_state in (None, "unknown", "unavailable"):
            return False
        if self.door_sensor_inverted:
            return door_state in ("off", "open")
        return door_state in ("on", "open")

    def _finish_anchor(self):
        """When the wash actually finished, used as the floor for the door-history and
        announce-freshness windows. Preference order (D1, 2026-08-19 adversarial pass - the
        plain last_high_energy_at fallback used to collapse to "now - anti_crease_window_minutes"
        (8min) whenever last_high was None or boot-seeded, silently excluding an older door edge
        or deflating freshness latency):
          (a) self._finish_anchor_override, if set - the reconcile path's structural guarantee
              that Sonos can never fire on a boot-time conclusion (see _restore_reconcile).
          (b) last_high_energy_at, as before - stamped on every high-power sample, so at a real
              finish it is the end-of-programme marker.
          (c) while Unemptied/Emptied, self._store_state_since - the moment we entered that
              state is itself a post-finish marker, so any door edge after it is definitely an
              emptying. Covers a restored Unemptied whose store payload lacked last_high_energy_at
              (see the Unemptied boot branch in initialize(), which restores last_high_energy_at
              AND start_time from the store when present - this is the fallback for when it
              wasn't).
          (d) start_time + addload_window_minutes - physics: the drum door is interlocked shut
              past the add-load window, so any recorded open after that moment means the machine
              had finished (or was aborted). This also inflates freshness latency enough that a
              None/boot-recent last_high_energy_at can never fire Sonos, only push.
          (e) last resort: now - max(anti_crease_window_minutes, restore_corroboration_window_minutes)
              - only reached with no override, no last_high, no state marker and no start_time at
              all.
        Robust to a partially-initialised app throughout (getattr defaults) - production always
        has these set."""
        override = getattr(self, "_finish_anchor_override", None)
        if override is not None:
            return override
        anchor = getattr(self, "last_high_energy_at", None)
        if anchor is not None:
            return anchor
        if getattr(self, "state", None) in ("Unemptied", "Emptied"):
            since = getattr(self, "_store_state_since", None)
            if since is not None:
                return since
        start_time = getattr(self, "start_time", None)
        if start_time is not None:
            addload = getattr(self, "addload_window_minutes", 5) or 5
            return start_time + timedelta(minutes=addload)
        window = max(
            getattr(self, "anti_crease_window_minutes", 8) or 8,
            getattr(self, "restore_corroboration_window_minutes", 10) or 10,
        )
        return self._now_utc() - timedelta(minutes=window)

    def _door_open_edge_since(self, since_dt) -> bool:
        """True if the recorder shows the door OPEN at any moment strictly after since_dt.

        The recorder, not get_state, is the arbiter here: AD 4.5.13 can serve minutes-stale
        live state, but history is durable. During a wash the door is interlocked shut, so any
        open reading logged after the finish anchor is a fresh open edge (the human), never a
        leftover from before the cycle - and it is caught even if the contact has since read
        closed again (the ajar-door case). Best-effort: any history failure returns False."""
        if not getattr(self, "door_sensor", None) or since_dt is None:
            return False
        try:
            hist = self.get_history(
                entity_id=self.door_sensor,
                start_time=since_dt,
                end_time=self._now_utc(),
            )
            for entry in self._flatten_history(hist, self.door_sensor):
                state = entry.get("state")
                if state in (None, "unknown", "unavailable"):
                    continue
                t = whist.parse_utc(entry.get("last_changed") or entry.get("last_updated"))
                if t is None or t <= since_dt:
                    continue
                is_open = state in ("off", "open") if self.door_sensor_inverted else state in ("on", "open")
                if is_open:
                    return True
            return False
        except Exception as e:
            self.log(f"Door-open history check failed: {e}", level="DEBUG")
            return False

    def _finish_route_to_emptied(self) -> bool:
        """At a power/timer/backstop-driven finish (skip_announce=False), decide whether the
        human is already at/done with the machine so we go straight to Emptied and never
        announce: True if the door is physically open right now, or the recorder shows a
        door-open edge since the finish anchor. Never raises - degrades to False (announce as
        before) on any missing-attribute/IO problem."""
        try:
            if self._door_is_physically_open():
                return True
            return self._door_open_edge_since(self._finish_anchor())
        except Exception as e:
            self.log(f"Finish door-route check failed: {e}", level="DEBUG")
            return False

    def _finish_detection_latency_minutes(self) -> float:
        """Minutes between when the wash actually finished (the finish anchor) and now - how
        late this finish was detected. Feeds the announce freshness gate (FIX 3). 0.0 on any
        problem, so a broken clock never suppresses a real announcement."""
        try:
            anchor = self._finish_anchor()
            return (self._now_utc() - anchor).total_seconds() / 60 if anchor else 0.0
        except Exception:
            return 0.0

    def _door_state_changed(self, entity, attr, old, new, kwargs):
        """Handle door open and close events.
        Standard door contact (HA device_class: door): open door = "on", closed door = "off".
        Set door_sensor_inverted: true only if your sensor reports "off" when door opens (raw Zigbee contact sensor).
        """
        current_state = self.get_state(self.state_entity)
        if current_state is None or current_state in ("unknown", "unavailable"):
            current_state = self.state

        # Standard (HA device_class: door): on/open = door opened, off/closed = door closed
        if self.door_sensor_inverted:
            door_opened = new in ("off", "open")
            door_closed = new in ("on", "closed")
        else:
            door_opened = new in ("on", "open")
            door_closed = new in ("off", "closed")

        if door_opened:
            if self._door_previous_state_unreliable(old):
                self.log(
                    f"Ignoring door 'open' (new={new!r}) after unreliable old={old!r} "
                    f"— likely HA/entity restore, not a physical open",
                    level="DEBUG",
                )
                return
            self._handle_door_opened(current_state)
        elif door_closed:
            if self._door_previous_state_unreliable(old):
                self.log(
                    f"Ignoring door 'closed' (new={new!r}) after unreliable old={old!r} "
                    f"— likely HA/entity restore, not a physical close (last_door_closed_at unchanged)",
                    level="DEBUG",
                )
                return
            self._handle_door_closed(current_state)

    def _door_lock_state_changed(self, entity, attr, old, new, kwargs):
        """When door lock goes to unlocked and we're Unemptied, announce (washer ready to empty)."""
        if new not in ("unlocked", "off"):
            return
        if self.state != "Unemptied":
            return
        if self.notification_sent:
            return
        # FIX 1a: stay silent while a restore is still uncorroborated (see initialize()).
        if getattr(self, "restored_uncorroborated", False):
            return
        announce_enabled = True
        if self.announce_entity:
            try:
                announce_enabled = self.get_state(self.announce_entity) == "on"
            except Exception:
                pass
        if self.sonos_notifier and announce_enabled:
            try:
                self.sonos_notifier.notify(message=self.announce_message)
                self.log("Washer announcement sent (door unlocked)", level="INFO")
                self.notification_sent = True
            except Exception as e:
                self.log(f"Error sending notification: {e}", level="ERROR")

    def _handle_door_opened(self, current_state):
        """Handle door opening.

        Manual p.22: once temperature/water reach a level (typically after first heating), adding
        laundry is no longer possible - we must not treat door-open as AddLoad after observed_heating.

        - If run_time <= addload_window_minutes AND not observed_heating: Paused (AddLoad).
        - If run_time > addload_window_minutes: door open always means the wash had finished before the door
          opened - typical for European front-load washers (not top-load): the door stays locked while running.
          -> Unemptied -> Emptied -> Off (same flow as emptying complete; clears Paused-style UI without waiting for door close).
        - If within addload window but heating already started: use power pattern to decide done vs Paused
          (mid-cycle door check rare; power gate still useful there).
        """
        if current_state is None or current_state in ("unknown", "unavailable"):
            current_state = self.state
        state_normalized = (current_state or "").strip()

        current_power = self._get_current_power()
        run_minutes = self._get_run_duration_minutes() if self.start_time else 0
        self.log(f"Door opened, state: {current_state}, power: {current_power:.1f}W, run_time: {run_minutes:.1f} min", level="DEBUG")

        if state_normalized in ("Off", "Emptied"):
            self.door_fast_start_armed_until = None
            self.high_power_counter = 0

        if state_normalized == "Running":
            self.door_opened_time = self._now_utc()
            self.door_opened_during_cycle = True

            addload_ok = (
                run_minutes <= self.addload_window_minutes
                and not self.observed_heating
            )
            if addload_ok:
                self.log(
                    f"Door opened during Running (run_time: {run_minutes:.1f} min <= {self.addload_window_minutes} min, "
                    f"no heating yet) -> Paused (AddLoad)",
                    level="INFO",
                )
                self._transition_to_paused()
            else:
                if run_minutes <= self.addload_window_minutes and self.observed_heating:
                    self.log(
                        f"Door opened at {run_minutes:.1f} min but heating already started - "
                        f"AddLoad not possible (manual p.22); using power to decide done vs pause",
                        level="INFO",
                    )
                # Past addload window: front-loader door only unlocks when cycle allows (finished or brief add window).
                if run_minutes > self.addload_window_minutes:
                    # History backstop: catch a delayed-start wait the live detection missed (e.g.
                    # heating started right after the plateau, closing the gate before a resume
                    # tick) before handing off to _transition_to_unemptied.
                    if self.detect_delayed_start and not self._delayed_start_trimmed and self.start_time:
                        resume = self._infer_cycle_start_from_power_history(
                            self.start_time - timedelta(minutes=5), self._now_utc()
                        )
                        if resume and (resume - self.start_time).total_seconds() / 60 >= self.delay_plateau_minutes:
                            self.log(
                                f"Delayed start (history backstop): sliding cycle start "
                                f"{self._strftime_local(self.start_time)} -> {self._strftime_local(resume)}",
                                level="INFO",
                            )
                            self.start_time = resume
                            self._delayed_start_trimmed = True
                    self.log(
                        f"Door opened after {run_minutes:.1f} min (> {self.addload_window_minutes} min addload window) - "
                        f"front-load: finished before door -> Unemptied, Emptied, then Off",
                        level="INFO",
                    )
                    self._pending_end_reason = "door_opened_first"
                    self._transition_to_unemptied(skip_announce=True)
                    self._transition_to_emptied("Door opened - emptying")
                    self._transition_to_off(
                        "Door opened after wash complete (EU front-load) - cycle complete",
                        force=True,
                    )
                    return
                # Within window but not AddLoad (heating started): power gate only for this edge case.
                power_ok, mean_w, peak_w = self._power_looks_like_cycle_end()
                if power_ok:
                    self.log(
                        f"Door opened at {run_minutes:.1f} min, power looks like cycle end (mean={mean_w:.1f}W peak={peak_w:.1f}W) -> Unemptied then Emptied",
                        level="INFO",
                    )
                    self._pending_end_reason = "door_opened_first"
                    self._transition_to_unemptied(skip_announce=True)
                    self._transition_to_emptied("Door opened - emptying")
                else:
                    self.log(
                        f"Door opened at {run_minutes:.1f} min (within addload window, heating started) but power does not look like cycle end "
                        f"(mean={(mean_w or 0):.1f}W peak={(peak_w or 0):.1f}W) -> Paused (close door to continue)",
                        level="INFO",
                    )
                    self._transition_to_paused(reason_override="Door opened (machine may still be running - close door to continue)")

        elif state_normalized == "Paused":
            self.door_opened_time = self._now_utc()

        elif state_normalized == "Unemptied":
            # Door opened after notification - user is emptying
            self._transition_to_emptied("Door opened - emptying")

    def _handle_door_closed(self, current_state):
        """Handle door closing event."""
        if current_state is None or current_state in ("unknown", "unavailable"):
            current_state = self.state
        state_normalized = (current_state or "").strip()

        self.last_door_closed_at = self._now_utc()
        self.last_door_closed_trusted = True
        self.log(f"Door closed, current state: {current_state}", level="DEBUG")

        if state_normalized == "Paused":
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None

            current_power = self._get_current_power()

            if current_power >= self.start_w:
                # Door-close from Paused is authoritative and may happen seconds after
                # Paused was entered; bypass cooling so we don't get stuck in AddLoad.
                self._transition_to_running_from_pause(force=True)
            else:
                # Same rationale as above: pause-exit decision is driven by a real
                # door-close event and must not be blocked by cooling.
                self._evaluate_pause_exit(force=True)
        
        elif state_normalized == "Emptied":
            # Door closed after emptying - cycle complete, go to Off
            self.log(f"Door closed after emptying -> Off", level="INFO")
            self._safe_cancel_timer(self.emptied_watchdog_timer)
            self.emptied_watchdog_timer = None
            self._transition_to_off("Door closed - emptying complete", force=True)
            now = self._now_utc()
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

            # Auto-analyze after cycle completes (if enabled)
            if self.args.get("auto_analyze_cycles", False):
                self.run_in(self._auto_analyze_after_cycle, 300)  # Wait 5 min for data to settle

        elif state_normalized == "Off":
            now = self._now_utc()
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

    def _transition_to_paused(self, reason_override=None):
        """Transition to Paused state when door opens during Running (AddLoad or user interaction)."""
        if self._should_change_state("Paused", force=True):  # Door event bypasses cooling
            self.state = "Paused"
            # Merge into existing attrs so persisted Running-state fields (cycle_start_time,
            # energy_at_start, programme_confirmed_by_user, expected_dur_at_start, last_off_at,
            # etc.) survive the pause and are available on resume or restore.
            try:
                full = self.get_state(self.state_entity, attribute="all") or {}
                pause_attrs = dict((full.get("attributes") or {}))
            except Exception:
                pause_attrs = {}
            pause_attrs["reason"] = reason_override or "Door opened during cycle (AddLoad)"
            pause_attrs["run_time_minutes"] = round(self._get_run_duration_minutes(), 1)
            pause_attrs["energy_used"] = round(self._get_energy_used(), 3)
            # run_time_minutes/energy_used can legitimately be 0 here (AddLoad fires early in the
            # cycle, before heating starts) -- AppDaemon 4.5.13 set_state bug, not ours; see
            # smart_cooling.py's _publish() for details.
            self._set_state_entity( state="Paused", attributes=pause_attrs)
            r = pause_attrs.get("reason", "Paused")
            self.log(f"State -> Paused: {r}", level="INFO")

            # Timeout - if door stays open too long, assume emptied
            self.pause_timer = self.run_in(
                self._pause_timeout,
                self.pause_timeout_minutes * 60
            )

    def _pause_timeout(self, kwargs):
        """Called when door has been open for too long during a pause."""
        current_state = self.get_state(self.state_entity)

        if current_state == "Paused":
            self.log(f"Pause timeout after {self.pause_timeout_minutes} min - assuming emptied", level="INFO")
            self._transition_to_off("Door open too long during pause")

    def _running_watchdog_timeout(self, kwargs):
        """Safety watchdog - cycle running too long indicates sensor issue."""
        current_state = self.get_state(self.state_entity)
        if current_state == "Running":
            run_hours = self._get_run_duration_minutes() / 60
            self.log(f"WATCHDOG: Running for {run_hours:.1f}h exceeds max {self.max_running_hours}h - forcing Off", level="WARNING")
            self._transition_to_off(f"Watchdog: ran {run_hours:.1f}h (max {self.max_running_hours}h)")
        self.running_watchdog_timer = None

    def _unemptied_watchdog_timeout(self, kwargs):
        """Safety watchdog - Unemptied too long, user may have emptied without door sensor detecting."""
        current_state = self.get_state(self.state_entity)
        if current_state == "Unemptied":
            self.log(f"WATCHDOG: Unemptied for {self.unemptied_timeout_hours}h - assuming emptied", level="WARNING")
            # Goes straight to Off, bypassing _transition_to_emptied entirely - no door event was
            # ever seen, so there is nothing to patch emptied_by/emptied_ts onto. The saved
            # record's emptied_by stays whatever it was at Unemptied-save time (never observed):
            # correct, since nobody was actually seen emptying it.
            self._transition_to_off(f"Watchdog: unemptied timeout ({self.unemptied_timeout_hours}h)")
        self.unemptied_watchdog_timer = None
        self._safe_cancel_timer(self.unemptied_door_recheck_timer)
        self.unemptied_door_recheck_timer = None

    def _emptied_watchdog_timeout(self, kwargs):
        """User left the door open after emptying (common - they leave it open to dry the drum).
        After emptied_timeout_minutes we assume the cycle is fully done and transition to Off."""
        self.emptied_watchdog_timer = None
        current_state = self.get_state(self.state_entity)
        if current_state == "Emptied":
            self.log(
                f"Emptied for {self.emptied_timeout_minutes:.0f} min with door still open - "
                f"assuming done, transitioning to Off",
                level="INFO",
            )
            self._transition_to_off(f"Emptied watchdog: door left open ({self.emptied_timeout_minutes:.0f} min)")

    def _unemptied_door_recheck(self, kwargs):
        """While Unemptied, re-check door state every 60s so we don't miss an open event.
        Also check power: if high, we falsely declared done - recover to Running."""
        self.unemptied_door_recheck_timer = None
        current_state = self.get_state(self.state_entity)
        if current_state != "Unemptied":
            return
        try:
            pw = self.get_state(self.power_sensor)
            if pw not in (None, "unknown", "unavailable"):
                watts = float(pw or 0)
                if watts >= self.start_w:
                    self._recover_from_false_unemptied(watts)
                    return
        except (ValueError, TypeError):
            pass
        if self._door_is_physically_open():
            self.log("Door recheck: door is open while Unemptied -> Emptied (recovered missed event)", level="INFO")
            self._transition_to_emptied("Door opened - emptying (recheck)")
            return
        # FIX 4 (2026-08-19): the door may have been opened AND closed again (ajar) while we were
        # slow, so the live contact reads closed now but the human already emptied - the exact
        # wedge from the 2026-08-19 incident, where Unemptied's only exits needed a door-open edge
        # the ajar contact never produced. Ask the recorder (durable, unlike AD's stale live
        # state) for a door-open edge since the finish anchor. Rate-limited to ~5 min so this is
        # not a history call on every 60s tick.
        now = self._now_utc()
        last_check = self._unemptied_last_history_check_at
        if last_check is None or (now - last_check).total_seconds() >= 300:
            self._unemptied_last_history_check_at = now
            if self._door_open_edge_since(self._finish_anchor()):
                self.log(
                    "Door recheck: recorder shows a door-open edge since finish (contact reads "
                    "closed now - ajar) -> Emptied",
                    level="INFO",
                )
                self._transition_to_emptied("Door opened - emptying (history recheck)")
                return
        self.unemptied_door_recheck_timer = self.run_in(self._unemptied_door_recheck, 60)

    def _transition_to_running_from_pause(self, force=False):
        """Resume Running state after pause. Push current cycle_start_time/started_at_display
        so the entity is never left with stale or missing start time (fixes timestamp not updating)."""
        if self._should_change_state("Running", force=force):
            self.state = "Running"
            self.door_opened_time = None
            self.in_finishing_tail = False
            self.in_finishing_tail_entered_at = None
            self.last_tail_pulse_at = None
            self.tail_pattern_locked = False
            self.tail_pattern_cycle_seconds = None
            self.tail_pattern_last_pulse_at = None
            self.tail_pattern_locked_at = None
            # Push state and start-time attributes so UI always shows current cycle (not stale from entity)
            try:
                full = self.get_state(self.state_entity, attribute="all")
                attrs = dict((full or {}).get("attributes") or {})
            except Exception:
                attrs = {}
            if self.start_time:
                attrs["cycle_start_time"] = self._format_utc(self.start_time)
                attrs["cycle_start_time_local"] = self._format_local(self.start_time)
                attrs["started_at_display"] = self.start_time.astimezone(self._local_tz()).strftime("%H:%M")
            self._set_state_entity( state="Running", attributes=attrs)
            self.log("State -> Running (resumed after pause)", level="INFO")

            if not self.poll_timer:
                poll_interval = int(self.args.get("poll_interval_s", 60))
                self.poll_timer = self.run_in(self._poll_power, poll_interval)

    def _evaluate_pause_exit(self, force=False):
        """Determine whether to go to Unemptied or Off when exiting Paused state."""
        if not self._is_valid_completed_cycle():
            self._transition_to_off("Cycle interrupted or incomplete", force=force)
            return
        run_min = self._get_run_duration_minutes()
        prog, temp = self._classify_programme() if self.start_time else ("unknown", None)
        guard_dur = self._get_guard_duration(tick_prog=prog, tick_temp=temp, tick_class=(prog, temp))
        if not self._meets_finish_time_guards(run_min, guard_dur or 0):
            min_run = self._get_finish_min_run_minutes()
            self.log(
                f"Pause exit: valid cycle but finish guards not met (run {run_min:.0f}min, need >= {min_run:.0f}min and {self.finish_guard_fraction*100:.0f}% of expected) - treating as incomplete",
                level="INFO",
            )
            self._transition_to_off("Cycle incomplete (pause exit before finish time guards)", force=force)
            return
        # User opened door before we detected finish; record so feedback stores door_opened_first.
        if self.door_opened_during_cycle:
            self._pending_end_reason = "door_opened_first"
        self._transition_to_unemptied(force=force)

    def _transition_to_off(self, reason, force=False):
        """Transition to Off state.
        
        Args:
            reason: Reason for transition
            force: If True, bypass cooling period (for authoritative events like door open)
        """
        if self._should_change_state("Off", force=force):
            # Check if we had a valid cycle before resetting
            had_valid_cycle = False
            if self.start_time is not None:
                run_minutes = self._get_run_duration_minutes()
                if run_minutes >= self.min_cycle_minutes:
                    had_valid_cycle = True
            
            self.state = "Off"
            # Clear Running-specific attributes so the UI does not show previous cycle's
            # start time, ETA or progress. Use empty string so HA actually replaces the
            # value (None can leave the old value in place in some setups).
            now_utc = self._now_utc()
            clear_attrs = {
                "cycle_start_time": "",
                "cycle_start_time_local": "",
                "started_at_display": "",
                "estimated_end_time": "",
                "estimated_remaining_min": None,
                "elapsed_minutes": None,
                "progress_pct": None,
                "programme_duration_min": None,
                "programme_label": "",
                "detected_programme": "",
                "detected_temperature": "",
                "predicted_programme": "",
                "predicted_programme_label": "",
                "predicted_temperature": "",
                "energy_at_start": None,
                "last_high_energy_at": "",
                # Clear confirmation flags so next cycle starts fresh (prevents carry-over).
                "programme_confirmed_by_user": False,
                "programme_confirmed_by": "",
                "expected_dur_at_start": "",
                "expected_dur_key": "",
                # Cleared every time a cycle ends, same as the confirmation flags above - the
                # next cycle's _begin_running_cycle re-attributes from scratch.
                "started_by": "",
                "started_by_method": "",
                # Persist so after restart we can clamp restored start_time (no start before last Off/door close)
                "last_door_closed_at": self._format_local(self.last_door_closed_at) if self.last_door_closed_at else "",
                "last_door_closed_trusted": False,
                "last_off_at": self._format_local(now_utc),
            }
            # programme_confirmed_by_user/last_door_closed_trusted are always False here (cleared
            # every time a cycle ends) -- AppDaemon 4.5.13 set_state bug, not ours; see
            # smart_cooling.py's _publish() for details.
            self._set_state_entity( state="Off", attributes=clear_attrs)
            self.last_door_closed_trusted = False
            self.log(f"State -> Off ({reason})", level="INFO")
            
            # Auto-analyze after cycle completes (if enabled and we had a valid cycle)
            if self.args.get("auto_analyze_cycles", False) and had_valid_cycle:
                self.run_in(self._auto_analyze_after_cycle, 300)  # Wait 5 min for data to settle
            
            self._reset_cycle_tracking()

    def _compute_final_and_confirmed_programme(
        self, run_minutes: float, energy_used: float, update_detected: bool = True
    ) -> tuple:
        """Compute final (predicted) and user-confirmed programme + temperature.

        Returns (final_prog, final_temp, confirmed_prog, confirmed_temp).
        """
        final_prog = self.detected_programme
        final_temp = self.detected_temperature

        # Post-hoc eco refinement based on actual run duration + energy
        if final_prog == "eco":
            if run_minutes < 85 and 0.12 <= energy_used < 0.40:
                final_prog = "finvask"
                final_temp = None
                if update_detected:
                    self.detected_programme = "finvask"
                    self.detected_temperature = None
                self.log(f"Programme refined eco -> finvask (run {run_minutes:.0f}min, energy {energy_used:.2f}kWh)", level="INFO")
            elif run_minutes < 135 and energy_used < 0.52:
                final_prog = "strygelet"
                final_temp = None
                if update_detected:
                    self.detected_programme = "strygelet"
                    self.detected_temperature = None
                self.log(f"Programme refined eco -> strygelet (run {run_minutes:.0f}min, energy {energy_used:.2f}kWh)", level="INFO")
            elif 140 <= run_minutes < 195 and 0.55 <= energy_used < 0.90:
                final_prog = "bomuld"
                final_temp = "40°C"
                if update_detected:
                    self.detected_programme = "bomuld"
                    self.detected_temperature = "40°C"
                self.log(f"Programme refined eco -> bomuld 40C (run {run_minutes:.0f}min, energy {energy_used:.2f}kWh)", level="INFO")

        confirmed_prog = final_prog
        confirmed_temp = final_temp
        if self.confirm_entity:
            try:
                prog_label = self.get_state(self.confirm_entity)
                if prog_label and prog_label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog_key = self._LABEL_TO_KEY.get(prog_label, final_prog)
                    if prog_key:
                        confirmed_prog = prog_key
                    confirmed_temp = self._read_temperature_selector() or final_temp
            except Exception as e:
                self.log(f"Could not read confirm_entity for feedback (using predicted): {e}", level="DEBUG")
        # Only persist temperature for programmes that have by_temperature (e.g. bomuld).
        # Otherwise learning would use keys like uld|30°C but ETA asks _get_programme_duration("uld", None).
        if confirmed_temp and not self._programme_has_temperature(confirmed_prog):
            confirmed_temp = None
        return (final_prog, final_temp, confirmed_prog, confirmed_temp)

    def _get_selected_options(self):
        """Read optional option entities (water_plus, soak, prewash, short) for feedback storage."""
        opts = {}
        for name, entity in (
            ("water_plus", getattr(self, "option_water_plus_entity", None)),
            ("soak", getattr(self, "option_soak_entity", None)),
            ("prewash", getattr(self, "option_prewash_entity", None)),
            ("short", getattr(self, "option_short_entity", None)),
        ):
            if not entity:
                continue
            try:
                state = self.get_state(entity)
                if state not in (None, "unknown", "unavailable"):
                    opts[name] = state
            except Exception:
                pass
        return opts if opts else None

    def _get_spin_rpm_for_feedback(self) -> int | None:
        """Read spin_entity for feedback record. Returns rpm or None. Tolerates get_state failure."""
        if not self.spin_entity:
            return None
        try:
            spin_val = self.get_state(self.spin_entity)
            if spin_val and spin_val not in ("unknown", "unavailable", "—"):
                return self._parse_spin_rpm(spin_val)
        except Exception:
            pass
        return None

    def _get_confirmed_programme_duration_hint(self) -> float | None:
        """If the user has confirmed the programme, return its typical duration in minutes."""
        if not self.confirm_entity:
            return None
        try:
            prog_label = self.get_state(self.confirm_entity)
            if not prog_label or prog_label in ("Auto (unconfirmed)", "unknown", "unavailable"):
                return None
            prog_key = self._LABEL_TO_KEY.get(prog_label)
            if prog_key:
                temp = self._read_temperature_selector()
                return float(self._get_programme_duration(prog_key, temp))
        except Exception:
            pass
        return None

    def _get_programme_duration_hint_for_history(self) -> float | None:
        """Best duration hint for history correction: confirmed programme first, else detected.
        So we always pick the right 'cycle end' drop even when user didn't set the dropdown."""
        hint = self._get_confirmed_programme_duration_hint()
        if hint is not None:
            return hint
        if getattr(self, "detected_programme", None) and self.detected_programme not in ("unknown", ""):
            try:
                return float(self._get_programme_duration(self.detected_programme, getattr(self, "detected_temperature", None)))
            except (TypeError, ValueError):
                pass
        return None

    def _clear_cycle_ended_entity(self):
        """Clear the cycle_ended_at helper via the correct HA service so state persists and automations see it."""
        if not self.cycle_ended_at_entity:
            return
        try:
            domain = self.cycle_ended_at_entity.split(".", 1)[0] if "." in self.cycle_ended_at_entity else ""
            if domain == "input_text":
                self.call_service("input_text/set_value", entity_id=self.cycle_ended_at_entity, value="")
            elif domain == "input_datetime":
                self.call_service(
                    "input_datetime/set_datetime",
                    entity_id=self.cycle_ended_at_entity,
                    date="1970-01-01",
                    time="00:00:00",
                )
            else:
                self.set_state(self.cycle_ended_at_entity, state="")
        except Exception as e:
            self.log(f"Could not clear {self.cycle_ended_at_entity}: {e}", level="DEBUG")

    def _get_user_cycle_end_time(self):
        """If cycle_ended_at_entity is set, parse it as the exact cycle end time (local), return as UTC datetime or None.
        Supports input_datetime (ISO or "YYYY-MM-DD HH:MM:SS" in local time) or input_text ("HH:MM" = local on start date).
        Treats epoch (1970-01-01) or any year < 2000 as 'unset' so we don't log 'outside window' after clearing."""
        if not self.cycle_ended_at_entity or not self.start_time:
            return None
        try:
            raw = self.get_state(self.cycle_ended_at_entity)
            if not raw or raw in ("unknown", "unavailable", ""):
                return None
            raw = str(raw).strip()
            # Epoch or pre-2000 date is our clear sentinel; treat as unset
            if raw.startswith("1970-") or (len(raw) >= 4 and raw[:4].isdigit() and int(raw[:4]) < 2000):
                return None
            tz = self._local_tz()
            # If it has an explicit UTC offset or Z, use _parse_utc.
            if raw.endswith("Z") or "+" in raw[-7:] or (len(raw) >= 6 and raw[-6] in "+-" and raw[-3] == ":"):
                dt = _parse_utc(raw)
                if dt is not None and dt.year >= 2000:
                    return dt
                return None
            # input_datetime often returns "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DDTHH:MM:SS" in local time (no TZ).
            if ("T" in raw or ("-" in raw[:10] and " " in raw)) and "+" not in raw and "-" not in raw[11:]:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = tz.localize(dt) if hasattr(tz, "localize") else dt.replace(tzinfo=tz)
                if dt.year < 2000:
                    return None
                return dt.astimezone(timezone.utc)
            # "HH:MM" or "H:MM" -> local time on cycle start date
            if len(raw) <= 5 and ":" in raw:
                parts = raw.split(":")
                if len(parts) == 2:
                    h, m = int(parts[0].strip()), int(parts[1].strip() if len(parts[1]) >= 1 else 0)
                    if 0 <= h <= 23 and 0 <= m <= 59:
                        start_local = self.start_time.astimezone(tz)
                        user_dt = start_local.replace(hour=h, minute=m, second=0, microsecond=0)
                        return user_dt.astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError):
            pass
        return None

    def _correct_duration(self, run_minutes_wall: float, log_prefix: str = "") -> tuple:
        """Correct wall-clock run duration using user cycle end time or HA history.

        Returns (run_minutes, duration_source) where duration_source is one of:
        "user_cycle_end", "history_corrected", or None (uncorrected).
        """
        run_minutes = run_minutes_wall
        duration_source = None
        pfx = f"{log_prefix}: " if log_prefix else ""

        user_end = self._get_user_cycle_end_time()
        if user_end is not None and self.start_time is not None:
            now_utc = self._now_utc()
            if self.start_time <= user_end <= now_utc + timedelta(minutes=2):
                run_minutes_user = (user_end - self.start_time).total_seconds() / 60
                max_reasonable = (now_utc - self.start_time).total_seconds() / 60 + 2
                if run_minutes_user >= self.min_cycle_minutes and run_minutes_user <= max_reasonable:
                    run_minutes = run_minutes_user
                    self.log(
                        f"{pfx}Using exact cycle end time from {self.cycle_ended_at_entity}: "
                        f"{self._strftime_local(user_end)} -> duration {run_minutes:.1f} min",
                        level="INFO",
                    )
                    try:
                        self._clear_cycle_ended_entity()
                    except Exception:
                        pass
                    duration_source = "user_cycle_end"
                else:
                    self.log(f"{pfx}Ignoring cycle_ended_at (duration {run_minutes_user:.1f} min out of range)", level="DEBUG")
            else:
                self.log(f"{pfx}Ignoring cycle_ended_at (outside start-now window)", level="DEBUG")

        if duration_source is None:
            duration_hint = self._get_programme_duration_hint_for_history()
            actual_end = self._estimate_cycle_end_from_history(expected_duration_min=duration_hint)
            if actual_end is not None and self.start_time is not None:
                run_minutes_actual = (actual_end - self.start_time).total_seconds() / 60
                if run_minutes_actual >= self.min_cycle_minutes and run_minutes_actual <= run_minutes:
                    delta = run_minutes - run_minutes_actual
                    if delta > 1.0:
                        hint_note = " (programme hint used for history)" if duration_hint else ""
                        self.log(
                            f"{pfx}Using HA history for duration: {run_minutes_actual:.1f} min "
                            f"(detection was {delta:.0f} min late){hint_note}",
                            level="INFO",
                        )
                    run_minutes = run_minutes_actual
                    duration_source = "history_corrected"
            elif self.start_time is not None and self.last_high_energy_at is not None:
                estimated_end = self.last_high_energy_at + timedelta(minutes=2)
                if estimated_end <= self._now_utc():
                    run_minutes_actual = (estimated_end - self.start_time).total_seconds() / 60
                    if run_minutes_actual >= self.min_cycle_minutes and run_minutes_actual <= run_minutes:
                        delta = run_minutes - run_minutes_actual
                        if delta > 1.0:
                            self.log(
                                f"{pfx}Using last_high_energy_at+2min for duration: "
                                f"{run_minutes_actual:.1f} min (detection was {delta:.0f} min late, no history)",
                                level="DEBUG",
                            )
                        run_minutes = run_minutes_actual
                        duration_source = "history_corrected"

        return (run_minutes, duration_source)

    def _remove_last_cycle_feedback(self):
        """Remove the last cycle from feedback (used when recovering from false Unemptied)."""
        import json
        import os
        if not os.path.exists(self.feedback_file):
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Could not read feedback for recovery: {e}", level="WARNING")
            return
        cycles = data.get("cycles") or []
        if not cycles:
            return
        removed = cycles.pop()
        confirmed = removed.get("confirmed", "")
        confirmed_temp = removed.get("confirmed_temperature")
        duration_min = removed.get("duration_min", 0)
        energy_kwh = removed.get("energy_kwh", 0)
        heating_bursts = removed.get("heating_bursts", 0)
        wfb.remove_learned_sample(
            self._learned_durations,
            self._history_centroids,
            wp.learn_key_for(self.PROGRAMME_PROFILES, confirmed, confirmed_temp),
            duration_min,
            energy_kwh,
            heating_bursts,
        )
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.log(f"Removed false cycle from feedback (was {duration_min:.0f}min, {confirmed})", level="INFO")
        except Exception as e:
            self.log(f"Could not write feedback after recovery: {e}", level="WARNING")

    def _recover_from_false_unemptied(self, watts: float):
        """Recover from false Unemptied: machine is still running (power high). Transition back to Running."""
        self.log(
            f"Recovering from false Unemptied: power {watts:.1f}W - machine still running, reverting to Running",
            level="WARNING",
        )
        self._remove_last_cycle_feedback()

        try:
            attrs = (self.get_state(self.state_entity, attribute="all") or {}).get("attributes") or {}
        except Exception:
            attrs = {}
        run_min = attrs.get("run_time_minutes") or 0
        if run_min <= 0:
            self.log("Cannot recover: no run_time_minutes on entity", level="WARNING")
            return
        now = self._now_utc()
        # Use last_door_closed_at as start_time only when trusted (not a restart artefact).
        # run_time_minutes is programme length (corrected), not wall-clock elapsed.
        trusted_door = self._attr_bool_true(attrs.get("last_door_closed_trusted"))
        last_door_str = attrs.get("last_door_closed_at") or ""
        if not last_door_str and self.last_door_closed_at and self.last_door_closed_trusted:
            last_door_str = self._format_local(self.last_door_closed_at)
        if last_door_str and trusted_door:
            last_door = _parse_utc(last_door_str)
            if last_door:
                self.start_time = last_door
                self.last_door_closed_at = last_door
                self.last_door_closed_trusted = True
                self.log(f"Recovery: using last_door_closed_at {self._format_local(last_door)} as start_time", level="INFO")
            else:
                self.start_time = now - timedelta(minutes=run_min)
        else:
            self.start_time = now - timedelta(minutes=run_min)
        self.detected_programme = attrs.get("detected_programme") or "unknown"
        self.detected_temperature = attrs.get("detected_temperature") or None
        self.heating_phase_count = int(attrs.get("heating_bursts", 0))
        self.max_power_seen = float(attrs.get("max_power_w", 0)) or watts
        self.observed_heating = self.heating_phase_count > 0
        # False finish may have persisted heating_bursts=0 - infer from power history (graph shows 2000W+).
        if self.heating_phase_count == 0:
            self._restore_heating_from_power_history()
        if self._pending_confirmed_by_user is not None:
            # entity attrs were already wiped by _transition_to_unemptied's "clear for next
            # load" step before this recovery ran - restore from the pre-clear stash instead.
            self.programme_confirmed_by_user = self._pending_confirmed_by_user
            self.confirmed_by_username = self._pending_confirmed_by
        else:
            self.programme_confirmed_by_user = bool(attrs.get("programme_confirmed_by_user"))
            self.confirmed_by_username = attrs.get("programme_confirmed_by") or None
        self._pending_confirmed_by_user = None
        self._pending_confirmed_by = None
        # This path reverts to Running without going through _begin_running_cycle, so
        # self._cycle_actor must be rebuilt from the entity the same way a restart restore does.
        self._cycle_actor = self._cycle_actor_from_state_attrs(attrs)
        self.expected_dur_at_start = None
        self._guard_bar_class = None
        self._live_class_key = None
        self._live_class_since = None
        # Prefer selector (manual duration) - never use stale/wrong expected_dur from entity.
        if self.confirm_entity:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
                    if prog and prog != "unknown":
                        self.expected_dur_at_start = self._get_programme_duration(prog, temp, use_learned=False)
                        if self.expected_dur_at_start:
                            self._guard_bar_class = (prog, temp)
            except Exception:
                pass
        if self.expected_dur_at_start is None and self.detected_programme and self.detected_programme != "unknown":
            self.expected_dur_at_start = self._get_programme_duration(
                self.detected_programme, self.detected_temperature, use_learned=False
            )
            if self.expected_dur_at_start:
                self._guard_bar_class = (self.detected_programme, self.detected_temperature)
        if self.expected_dur_at_start is None:
            self.expected_dur_at_start = self._get_guard_duration(
                self.detected_programme, self.detected_temperature, (self.detected_programme, self.detected_temperature)
            )
        try:
            energy_used = float(attrs.get("energy_used", 0) or 0)
            current_energy = self.get_state(self.energy_sensor)
            if current_energy not in (None, "unknown", "unavailable"):
                self.energy_start = float(current_energy) - energy_used
            else:
                self.energy_start = None
        except (ValueError, TypeError):
            self.energy_start = None
        self.finish_confirmed = False
        self.low_power_counter = 0
        self.low_power_start_time = None
        self.energy_stable_start_time = None
        self.last_high_energy_at = now
        self._zero_power_since = None
        # Reset so we can announce when the cycle truly finishes (the previous was a false finish).
        self.notification_sent = False
        self.in_finishing_tail = False
        self.in_finishing_tail_entered_at = None
        self.last_tail_pulse_at = None
        self.tail_pattern_locked = False
        self.tail_pattern_cycle_seconds = None
        self.tail_pattern_last_pulse_at = None
        self.tail_pattern_locked_at = None

        self.state = "Running"
        profile = self._get_profile(self.detected_programme, self.detected_temperature)
        guard_dur = self._get_guard_duration(
            self.detected_programme, self.detected_temperature, (self.detected_programme, self.detected_temperature)
        )
        elapsed = (now - self.start_time).total_seconds() / 60
        remaining = max(0, round(guard_dur - elapsed))
        est_end = self.start_time + timedelta(minutes=guard_dur)
        run_attrs = {
            "detected_programme": self.detected_programme,
            "detected_temperature": self.detected_temperature or "",
            "programme_label": profile.get("label", self.detected_programme),
            "cycle_complete": False,
            "run_time_minutes": None,
            "energy_used": round(self._get_energy_used(), 3),
            "end_reason": "",
            "idle_min": None,
            "heating_bursts": self.heating_phase_count,
            "max_power_w": round(self.max_power_seen, 0),
            "cycle_start_time": self._format_utc(self.start_time),
            "cycle_start_time_local": self._format_local(self.start_time),
            "started_at_display": self.start_time.astimezone(self._local_tz()).strftime("%H:%M"),
            "elapsed_minutes": round(elapsed, 1),
            "progress_pct": min(100, max(0, round(100 * elapsed / guard_dur))) if guard_dur else 0,
            "estimated_remaining_min": remaining,
            "estimated_end_time": est_end.astimezone(self._local_tz()).strftime("%H:%M"),
            "programme_duration_min": guard_dur,
            "programme_confirmed_by_user": self.programme_confirmed_by_user,
            "programme_confirmed_by": self.confirmed_by_username or "",
            "expected_dur_at_start": self.expected_dur_at_start or "",
            "expected_dur_key": self._guard_bar_key_str(),
        }
        if self.energy_start is not None:
            run_attrs["energy_at_start"] = self.energy_start
        if self.last_door_closed_at:
            run_attrs["last_door_closed_at"] = self._format_local(self.last_door_closed_at)
        run_attrs["last_door_closed_trusted"] = bool(self.last_door_closed_trusted)
        # cycle_complete is always False here; heating_bursts/estimated_remaining_min/
        # programme_confirmed_by_user/last_door_closed_trusted can also legitimately be 0/False
        # (cold programme, near end-of-cycle recovery, Auto mode, no trusted door-close) --
        # AppDaemon 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
        self._set_state_entity( state="Running", attributes=run_attrs)

        self._safe_cancel_timer(self.unemptied_watchdog_timer)
        self.unemptied_watchdog_timer = None
        self._safe_cancel_timer(self.unemptied_door_recheck_timer)
        self.unemptied_door_recheck_timer = None
        self.running_watchdog_timer = self.run_in(self._running_watchdog_timeout, int(self.max_running_hours * 3600))
        if self.use_energy_detection:
            self._start_energy_detection()
        if not self.poll_timer:
            poll_interval = int(self.args.get("poll_interval_s", 60))
            self.poll_timer = self.run_in(self._poll_power, poll_interval)
        if not self.history_poll_timer:
            interval = int(self.args.get("history_check_interval_s", 300))
            self.history_poll_timer = self.run_in(self._periodic_check_power_history, interval)

    def _transition_to_unemptied(self, skip_announce=False, force=False):
        """Transition to Unemptied state (cycle done, door still closed, waiting for user).

        skip_announce: If True, do not send the "washer ready to empty" notification.
        Set when the user opened the door before we detected finish - they already know
        the cycle is done, and announcing would be redundant (we failed to notify in time).
        """
        self.in_finishing_tail = False
        self.in_finishing_tail_entered_at = None
        self.last_tail_pulse_at = None
        self.tail_pattern_locked = False
        self.tail_pattern_cycle_seconds = None
        self.tail_pattern_last_pulse_at = None
        self.tail_pattern_locked_at = None
        # Gate: only allow transition when recent power looks like real cycle end (anti-crease or off), not mid-cycle rinse.
        # Skip when user opened door first (skip_announce) or when we already verified standby/cadence break
        # (standby_backstop = 5+ min of hard 0W - stronger evidence than the power-pattern gate itself).
        if not skip_announce and self._pending_end_reason not in ("tail_to_standby", "tail_pattern_break", "standby_backstop"):
            ok, mean_w, peak_w = self._power_looks_like_cycle_end()
            if not ok:
                if mean_w is not None and peak_w is not None:
                    self.log(
                        f"Blocking transition to Unemptied: power pattern does not look like cycle end "
                        f"(mean={mean_w:.1f}W peak={peak_w:.1f}W; need mean≤{self.finish_power_gate_max_mean_w:.0f}W peak≤{self.finish_power_gate_max_peak_w:.0f}W or mean≤{self.finish_power_gate_off_max_mean_w:.0f}W peak≤{self.finish_power_gate_off_max_peak_w:.0f}W)",
                        level="INFO",
                    )
                return
        # FIX 2 (2026-08-19): door-aware finish. This is a power/timer/backstop-driven finish
        # (skip_announce=False; the door-driven paths pass skip_announce=True and handle the door
        # themselves in _handle_door_opened). If the human is already at the machine - door open
        # now, or a recorder door-open edge since the finish anchor (the ajar case: opened while
        # we were slow, reads closed again now) - they have emptied it: go straight to Emptied
        # and never announce. Feedback is still saved exactly once, by _transition_to_emptied
        # while self.state is still Running (its came_from_running save). Cancel the running
        # watchdog here since we skip _transition_to_unemptied's own teardown.
        if not skip_announce and self._finish_route_to_emptied():
            self.log(
                "Finish with door already open (or a door-open edge since the finish anchor) - "
                "routing to Emptied instead of announcing Unemptied",
                level="INFO",
            )
            self._pending_end_reason = None
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
            self._transition_to_emptied("cycle finished (door already open at finish)")
            return
        if self._should_change_state("Unemptied", force=force):
            # History backstop: if delayed-start detection never caught the wait live (e.g. the
            # live gate closed before a resume tick, or the app restarted mid-wait), check power
            # history once more before computing run_minutes so the wait is not recorded as wash
            # time. Does not re-base energy accounting - the cycle is ending regardless.
            if self.detect_delayed_start and not self._delayed_start_trimmed and self.start_time:
                resume = self._infer_cycle_start_from_power_history(
                    self.start_time - timedelta(minutes=5), self._now_utc()
                )
                if resume and (resume - self.start_time).total_seconds() / 60 >= self.delay_plateau_minutes:
                    self.log(
                        f"Delayed start (history backstop): sliding cycle start "
                        f"{self._strftime_local(self.start_time)} -> {self._strftime_local(resume)}",
                        level="INFO",
                    )
                    self.start_time = resume
                    self._delayed_start_trimmed = True

            energy_used = self._get_energy_used()
            run_minutes_wall = self._get_run_duration_minutes()

            run_minutes, duration_source = self._correct_duration(run_minutes_wall)

            final_prog, final_temp, confirmed_prog, confirmed_temp = self._compute_final_and_confirmed_programme(
                run_minutes, energy_used, update_detected=True
            )
            final_profile = self._get_profile(final_prog, final_temp)

            spin_rpm = self._get_spin_rpm_for_feedback()

            # For warm programmes, backfill heating_bursts/max_power_w from history if live counters are implausible.
            feedback_hb = self.heating_phase_count
            feedback_max_w = self.max_power_seen
            if final_profile and final_profile.get("heats") and (self.heating_phase_count == 0 or self.max_power_seen < 500):
                bf_bursts, bf_max = self._backfill_heating_from_history_for_feedback()
                if bf_bursts is not None and bf_max is not None:
                    feedback_hb = max(self.heating_phase_count, bf_bursts)
                    feedback_max_w = max(self.max_power_seen, bf_max)
                    if feedback_hb > self.heating_phase_count or feedback_max_w > self.max_power_seen:
                        self.log(
                            f"Backfilled feedback heating: bursts {self.heating_phase_count} -> {feedback_hb}, max_power {self.max_power_seen:.0f} -> {feedback_max_w:.0f}W",
                            level="DEBUG",
                        )

            if duration_source is None:
                self.log(
                    "Duration not corrected from history; storing wall-clock duration. "
                    "ETAs may be late if the cycle ended before detection or door open.",
                    level="WARNING",
                )
            # Finish-precedence: use pending end reason (e.g. anti_crease_pattern, door_opened_first) if set
            if self._pending_end_reason:
                end_reason = self._pending_end_reason
                self._pending_end_reason = None
            elif skip_announce:
                end_reason = "door_opened_first"
            else:
                end_reason = "user_cycle_end" if duration_source == "user_cycle_end" else "low_power_detected"
            idle_min = (run_minutes_wall - run_minutes) if (duration_source and run_minutes_wall > run_minutes) else None
            # effective_end_at = when wash program finished; detected_at = when we transitioned (for learning/audit).
            effective_end_at_str = None
            detected_at_str = self._format_local(self._now_utc())
            if self.start_time and run_minutes is not None:
                effective_end_dt = self.start_time + timedelta(minutes=run_minutes)
                effective_end_at_str = self._format_local(effective_end_dt)
            # Classify for learning quality (validation does not block transition; it classifies the record).
            classification = self._classify_cycle_completion(
                run_minutes=run_minutes,
                energy_kwh=energy_used,
                heating_bursts=feedback_hb,
                max_power_w=feedback_max_w,
                predicted=final_prog,
                predicted_temperature=final_temp,
                confirmed=confirmed_prog,
                confirmed_temperature=confirmed_temp,
                transition_path=end_reason,
                spin_rpm=spin_rpm,
            )
            if self._delayed_start_trimmed and duration_source is None:
                duration_source = "delayed_start_trimmed"
            saved_record = self._save_cycle_feedback(
                predicted=final_prog,
                predicted_temperature=final_temp,
                confirmed=confirmed_prog,
                confirmed_temperature=confirmed_temp,
                duration_min=run_minutes,
                energy_kwh=energy_used,
                heating_bursts=feedback_hb,
                max_power_w=feedback_max_w,
                spin_rpm=spin_rpm,
                user_confirmed=self.programme_confirmed_by_user,
                spin_user_confirmed=(self.programme_confirmed_by_user and spin_rpm is not None),
                duration_source=duration_source,
                end_reason=end_reason,
                idle_min=idle_min,
                confirmed_by=self.confirmed_by_username,
                effective_end_at=effective_end_at_str,
                detected_at=detected_at_str,
                completion_class=classification["completion_class"],
                valid_for_learning=classification["valid_for_learning"],
                validation_flags=classification["validation_flags"],
                transition_path=classification["end_reason"],
                programme_key_used_for_validation=classification.get("programme_key_used_for_validation"),
                profile_version="1",
                validation_version="2",
                selected_options=self._get_selected_options(),
                cost_kr=self._session_cost_kr if self.track_cycle_cost else None,
                vibration=self._vibration_summary(),
                actor_start=self._cycle_actor,
            )
            if saved_record is not None:
                self._maybe_send_confirm_push(saved_record)
                self._schedule_vibration_unload_patch(saved_record)
                self._last_saved_record_ts = saved_record.get("ts")

            self.state = "Unemptied"
            confirmed_profile = self._get_profile(confirmed_prog, confirmed_temp)
            temp_str = f" {confirmed_temp}" if confirmed_temp else ""
            attributes = {
                "cycle_complete": True,
                "run_time_minutes": round(run_minutes, 1),
                "detected_programme": confirmed_prog,
                "detected_temperature": confirmed_temp or "",
                "programme_label": confirmed_profile.get("label", confirmed_prog),
                "heating_bursts": self.heating_phase_count,
                "max_power_w": round(self.max_power_seen, 0),
                # Clear Running progress bar so UI does not show previous cycle's start/ETA
                "cycle_start_time": "",
                "cycle_start_time_local": "",
                "estimated_end_time": "",
                "estimated_remaining_min": None,
                "elapsed_minutes": None,
                "progress_pct": None,
                "programme_duration_min": None,
                "energy_at_start": None,
                "last_high_energy_at": None,
            }
            # Dashboard visibility for who loaded the machine (see _begin_running_cycle /
            # _attribute); merge-not-replace set_state already carries this over from the
            # Running publish, but writing it explicitly here keeps it correct even after a
            # _recover_from_false_unemptied revert re-seeded self._cycle_actor mid-cycle.
            attributes["started_by"] = (self._cycle_actor or {}).get("person") or ""
            if energy_used > 0:
                attributes["energy_used"] = round(energy_used, 3)
            if spin_rpm is not None:
                attributes["spin_rpm"] = spin_rpm
            if end_reason:
                attributes["end_reason"] = end_reason
            if idle_min is not None and idle_min >= 0:
                attributes["idle_min"] = round(idle_min, 1)
            if self.last_door_closed_at:
                attributes["last_door_closed_at"] = self._format_local(self.last_door_closed_at)
                attributes["last_door_closed_trusted"] = bool(self.last_door_closed_trusted)
            if getattr(self, "_pending_tail_mean_w", None) is not None:
                attributes["tail_pattern_detected"] = True
                attributes["tail_window_mean_w"] = round(self._pending_tail_mean_w, 1)
                if getattr(self, "_pending_tail_std_w", None) is not None:
                    attributes["tail_window_std_w"] = round(self._pending_tail_std_w, 1)
                if getattr(self, "_pending_tail_peak_w", None) is not None:
                    attributes["tail_window_peak_w"] = round(self._pending_tail_peak_w, 1)
                self._pending_tail_mean_w = None
                self._pending_tail_std_w = None
                self._pending_tail_peak_w = None

            # Next load: clear confirmation + HA helpers now (was only cleared at Off before).
            # Stash the pre-clear value first: if this Unemptied turns out to be false (machine
            # still running), _recover_from_false_unemptied needs the real confirmation back, and
            # by then both the entity attrs and the confirm_entity selector are already wiped.
            self._pending_confirmed_by_user = self.programme_confirmed_by_user
            self._pending_confirmed_by = self.confirmed_by_username
            attributes["programme_confirmed_by_user"] = False
            attributes["programme_confirmed_by"] = ""
            attributes["expected_dur_at_start"] = ""
            attributes["expected_dur_key"] = ""

            # programme_confirmed_by_user is always False here (cleared for next load);
            # heating_bursts/last_door_closed_trusted can also be 0/False (cold programme,
            # no trusted door-close) -- AppDaemon 4.5.13 set_state bug, not ours; see
            # smart_cooling.py's _publish() for details.
            self._set_state_entity( state="Unemptied", attributes=attributes)

            self.programme_confirmed_by_user = False
            self.confirmed_by_username = None
            self.expected_dur_at_start = None
            self._guard_bar_class = None
            self._live_class_key = None
            self._live_class_since = None
            self._set_programme_helpers_default()

            if self.poll_timer:
                self._safe_cancel_timer(self.poll_timer)
                self.poll_timer = None
            if self.history_poll_timer:
                self._safe_cancel_timer(self.history_poll_timer)
                self.history_poll_timer = None

            # Cancel running watchdog, start unemptied watchdog
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = self.run_in(
                self._unemptied_watchdog_timeout,
                int(self.unemptied_timeout_hours * 3600)
            )
            # Reset the door-history rate-limiter so the first recheck after entry (~60s) does
            # a recorder lookback for an ajar-door edge (FIX 4), not just a live-contact peek.
            self._unemptied_last_history_check_at = None
            self.unemptied_door_recheck_timer = self.run_in(self._unemptied_door_recheck, 60)

            self.log(
                f"State -> Unemptied (ran {run_minutes:.1f} min, used {energy_used:.3f} kWh) "
                f"confirmed: {confirmed_prog}{self._log_safe(temp_str)}"
                + (f"  spin {spin_rpm} rpm" if spin_rpm is not None else ""),
                level="INFO",
            )

            # Send notification - washer done, please empty!
            # Skip when skip_announce=True (user opened door before we detected finish).
            # If door_lock_entity is set we announce when the door unlocks instead (see _door_lock_state_changed).
            announce_enabled = True
            if self.announce_entity:
                try:
                    announce_enabled = self.get_state(self.announce_entity) == "on"
                except Exception:
                    pass
            # FIX 1a: an uncorroborated restore stays silent until a live signal confirms the
            # cycle (see initialize()'s restore corroboration / _restore_reconcile).
            if (not skip_announce and not self.door_lock_entity and self.sonos_notifier
                    and not self.notification_sent and announce_enabled
                    and not getattr(self, "restored_uncorroborated", False)):
                # FIX 3: if we only detected the finish long after it happened (e.g. a restart
                # storm delayed detection), a Sonos blast about a wash emptied long ago is worse
                # than a quiet mobile push. notification_sent still gates against a double-notify.
                latency_min = self._finish_detection_latency_minutes()
                freshness_min = getattr(self, "announce_freshness_minutes", 20)
                # D2 (2026-08-19 adversarial pass follow-up): _restore_reconcile sets this for the
                # duration of its own transition call - an explicit guarantee that path can never
                # reach Sonos, instead of relying on the freshness-latency arithmetic to always
                # exceed freshness_min (see _restore_reconcile's docstring).
                force_push = getattr(self, "_announce_force_push", False)
                if latency_min > freshness_min or force_push:
                    self._push_mobile(
                        f"Washer finished about {latency_min:.0f} min ago (late detection) - "
                        f"ready to empty."
                    )
                    if latency_min > freshness_min:
                        self.log(
                            f"Finish detected {latency_min:.0f} min late (> {freshness_min:.0f} min) "
                            f"- mobile push instead of Sonos announcement",
                            level="INFO",
                        )
                    if force_push:
                        self.log(
                            "Push forced by the restore reconcile - never Sonos on a "
                            "boot-time conclusion, regardless of freshness timing",
                            level="INFO",
                        )
                    self.notification_sent = True
                else:
                    try:
                        self.sonos_notifier.notify(message=self.announce_message)
                        self.log("[TAIL] Washer announcement sent", level="INFO")
                        self.notification_sent = True
                    except Exception as e:
                        self.log(f"Error sending notification: {e}", level="ERROR")

    def _handle_force_emptied(self, event_name, data, kwargs):
        """washer_force_emptied (dashboard Emptied button): the drum is empty but the door
        contact never saw the emptying. Only honored from Unemptied - any earlier state may
        still be a live cycle, and _transition_to_emptied would save its feedback too soon."""
        data = data or {}
        if self.state != "Unemptied":
            self.log(
                f"Force Emptied ignored from state {self.state!r} (only valid from Unemptied)",
                level="WARNING",
            )
            return
        reason = data.get("reason") or "Forced via event"
        self.log(f"Force Emptied via event ({reason})", level="INFO")
        self._transition_to_emptied(f"Forced emptied ({reason})")

    def _transition_to_emptied(self, reason):
        """Transition to Emptied state (door open, user is emptying)."""
        if self._should_change_state("Emptied", force=True):  # Door event bypasses cooling
            # Attribution: captured as "now" - unlike the start anchor (which clamps back to
            # door-close, see _begin_running_cycle), emptying is observed as it happens.
            actor_empty = self._attribute("washer_emptied")
            energy_used = self._get_energy_used()
            run_minutes = self._get_run_duration_minutes()

            # Save feedback when we arrive in Emptied directly from Running (bypassing Unemptied).
            # This covers two paths:
            #   1. "cycle finished" reason  - explicit skip-Unemptied path
            #   2. In-memory state is still "Running" - _transition_to_unemptied was blocked
            #      (e.g. by _should_change_state) before _transition_to_emptied was called.
            # In both cases start_time and run_minutes are still set; feedback has not been saved yet.
            # Must check self.state, not the HA entity: right after _transition_to_unemptied the
            # entity read can still return "Running" (set_state round trip pending), which made
            # this guard pass again and save the same cycle twice seconds apart.
            came_from_running = (
                self.state == "Running"
                or "cycle finished" in reason
            )
            if (
                self.start_time is not None
                and run_minutes >= self.min_cycle_minutes
                and came_from_running
            ):
                try:
                    run_minutes_wall = run_minutes
                    run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Door-open")

                    final_prog, final_temp, confirmed_prog, confirmed_temp = self._compute_final_and_confirmed_programme(
                        run_minutes, energy_used, update_detected=False
                    )
                    final_profile = self._get_profile(final_prog, final_temp)
                    spin_rpm = self._get_spin_rpm_for_feedback()
                    feedback_hb = self.heating_phase_count
                    feedback_max_w = self.max_power_seen
                    if final_profile and final_profile.get("heats") and (self.heating_phase_count == 0 or self.max_power_seen < 500):
                        bf_bursts, bf_max = self._backfill_heating_from_history_for_feedback()
                        if bf_bursts is not None and bf_max is not None:
                            feedback_hb = max(self.heating_phase_count, bf_bursts)
                            feedback_max_w = max(self.max_power_seen, bf_max)
                    if duration_source is None:
                        self.log(
                            "Duration not corrected from history; storing wall-clock (door-open) duration. "
                            "ETAs may be late if you opened the door after the cycle ended.",
                            level="WARNING",
                        )
                    end_reason = "user_cycle_end" if duration_source == "user_cycle_end" else "door_opened_first"
                    idle_min = (run_minutes_wall - run_minutes) if (duration_source and run_minutes_wall > run_minutes) else None
                    effective_end_at_str = None
                    detected_at_str = self._format_local(self._now_utc())
                    if self.start_time and run_minutes is not None:
                        effective_end_dt = self.start_time + timedelta(minutes=run_minutes)
                        effective_end_at_str = self._format_local(effective_end_dt)
                    classification = self._classify_cycle_completion(
                        run_minutes=run_minutes,
                        energy_kwh=energy_used,
                        heating_bursts=feedback_hb,
                        max_power_w=feedback_max_w,
                        predicted=final_prog,
                        predicted_temperature=final_temp,
                        confirmed=confirmed_prog,
                        confirmed_temperature=confirmed_temp,
                        transition_path=end_reason,
                        spin_rpm=spin_rpm,
                    )
                    saved_record = self._save_cycle_feedback(
                        predicted=final_prog,
                        predicted_temperature=final_temp,
                        confirmed=confirmed_prog,
                        confirmed_temperature=confirmed_temp,
                        duration_min=run_minutes,
                        energy_kwh=energy_used,
                        heating_bursts=feedback_hb,
                        max_power_w=feedback_max_w,
                        spin_rpm=spin_rpm,
                        user_confirmed=self.programme_confirmed_by_user,
                        spin_user_confirmed=(self.programme_confirmed_by_user and spin_rpm is not None),
                        duration_source=duration_source,
                        end_reason=end_reason,
                        idle_min=idle_min,
                        confirmed_by=self.confirmed_by_username,
                        effective_end_at=effective_end_at_str,
                        detected_at=detected_at_str,
                        completion_class=classification["completion_class"],
                        valid_for_learning=classification["valid_for_learning"],
                        validation_flags=classification["validation_flags"],
                        transition_path=classification["end_reason"],
                        programme_key_used_for_validation=classification.get("programme_key_used_for_validation"),
                        profile_version="1",
                        validation_version="2",
                        selected_options=self._get_selected_options(),
                        cost_kr=self._session_cost_kr if self.track_cycle_cost else None,
                        vibration=self._vibration_summary(),
                        actor_start=self._cycle_actor,
                        actor_empty=actor_empty,
                    )
                    if saved_record is not None:
                        self._maybe_send_confirm_push(saved_record)
                        self._schedule_vibration_unload_patch(saved_record)
                        self._last_saved_record_ts = saved_record.get("ts")
                except Exception as e:
                    self.log(f"Could not save feedback on Emptied transition: {e}", level="WARNING")
            elif not came_from_running:
                # Path B (the canonical "someone emptied it"): the record was already written
                # by _transition_to_unemptied, possibly long before this door event - patch
                # emptied_by/emptied_ts onto it instead of guessing at a duplicate save.
                # self._last_saved_record_ts names it (set at both live _save_cycle_feedback
                # sites); _patch_cycle_record tolerates it being stale/absent.
                self._patch_cycle_record(self._last_saved_record_ts, {
                    "emptied_by": (actor_empty or {}).get("person"),
                    "emptied_ts": self._format_local(self._now_utc()),
                    "attribution": {"empty": actor_empty},
                })

            self.state = "Emptied"
            attributes = {
                "reason": reason,
                "run_time_minutes": round(run_minutes, 1) if run_minutes > 0 else None
            }
            # Dashboard visibility for who emptied the machine (see _attribute above).
            attributes["emptied_by"] = (actor_empty or {}).get("person") or ""
            if energy_used > 0:
                attributes["energy_used"] = round(energy_used, 3)
            # Preserve run_time_minutes, end_reason, idle_min from entity when coming from Unemptied
            # so we keep the corrected programme length in the UI instead of overwriting with wall-clock to door.
            try:
                full = self.get_state(self.state_entity, attribute="all")
                attrs = (full or {}).get("attributes") or {}
                if self.get_state(self.state_entity) == "Unemptied":
                    if attrs.get("run_time_minutes") is not None:
                        attributes["run_time_minutes"] = attrs["run_time_minutes"]
                    if attrs.get("end_reason"):
                        attributes["end_reason"] = attrs["end_reason"]
                    if attrs.get("idle_min") is not None:
                        attributes["idle_min"] = attrs["idle_min"]
            except Exception:
                pass

            self._set_state_entity( state="Emptied", attributes=attributes)
            
            # Cancel unemptied watchdog since we're now emptying
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None
            self._safe_cancel_timer(self.unemptied_door_recheck_timer)
            self.unemptied_door_recheck_timer = None
            if self.history_poll_timer:
                self._safe_cancel_timer(self.history_poll_timer)
                self.history_poll_timer = None
            # Start emptied watchdog - if door stays open (user leaves it to dry), transition to Off.
            self._safe_cancel_timer(self.emptied_watchdog_timer)
            self.emptied_watchdog_timer = self.run_in(
                self._emptied_watchdog_timeout,
                int(self.emptied_timeout_minutes * 60),
            )
            # Ensure poll_timer runs so we detect 0W even if listen_state doesn't fire
            # (e.g. power sensor already at 0W when entering Emptied - no state change event).
            if not self.poll_timer:
                poll_interval = int(self.args.get("poll_interval_s", 60))
                self.poll_timer = self.run_in(self._poll_power, poll_interval)

            # Re-enable announcements for the next cycle (user may have muted for this one)
            if self.announce_entity:
                try:
                    self.call_service("input_boolean/turn_on", entity_id=self.announce_entity)
                    self.log("Announce toggle reset to on for next cycle", level="DEBUG")
                except Exception as e:
                    self.log(f"Could not reset announce toggle: {e}", level="WARNING")
            
            self.log(f"State -> Emptied ({reason})", level="INFO")

    def _reset_cycle_tracking(self):
        """Reset all cycle-related tracking variables."""
        self.start_time = None
        self._start_time_source = None  # must not leak into the next cycle's block C rank gate
        self._cycle_id = None  # the store is already cleared by now (Off just called _set_state_entity)
        self._cycle_actor = None
        self.energy_start = None
        self._session_cost_kr = 0.0
        self._cost_prev_energy_kwh = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False
        self.program_timer = None
        self.low_power_counter = 0
        self.low_power_start_time = None
        self.high_power_counter = 0
        self.last_significant_power_at = None
        self.power_readings = []
        self.finish_confirmed = False
        self._zero_power_since = None
        self.notification_sent = False
        self.max_power_seen = 0.0
        self.observed_heating = False
        self.in_heating_phase = False
        self.heating_phase_count = 0
        self.detected_programme = "unknown"
        self.detected_temperature = None
        self.programme_confirmed_by_user = False
        self.confirmed_by_username = None
        self.expected_dur_at_start = None
        self._guard_bar_class = None
        self._live_class_key = None
        self._live_class_since = None
        self.in_finishing_tail = False
        self.in_finishing_tail_entered_at = None
        self.last_tail_pulse_at = None
        self.door_fast_start_armed_until = None
        self._delay_plateau_start = None
        self._delayed_start_trimmed = False
        self._delay_waiting = False
        self._delayed_start_lead_idle_min = None
        # A finished/off cycle can never be an uncorroborated restore of a live one - clear the
        # flag (and its rate-limiter) so it never leaks into the next cycle (FIX 1).
        self.restored_uncorroborated = False
        self._unemptied_last_history_check_at = None
        # D1: belt-and-braces - _restore_reconcile's own finally already clears this, but a cycle
        # ending must never carry a stale override into the next one under any path.
        self._finish_anchor_override = None
        # D2: same belt-and-braces as D1 above, for the force-push flag.
        self._announce_force_push = False
        self._reset_input_selectors()

    def _set_programme_helpers_default(self):
        """Reset HA programme / temperature / spin input_selects only (no timers).

        Call when a wash ends so the next load starts from Auto/—, not the previous cycle's picks.
        Service calls carry no user_id so _on_confirm_changed does not treat this as user confirmation.
        """
        try:
            if self.confirm_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.confirm_entity,
                    option="Auto (unconfirmed)",
                )
            if self.temperature_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.temperature_entity,
                    option="—",
                )
            if self.spin_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.spin_entity,
                    option="—",
                )
        except Exception as e:
            self.log(f"Could not reset programme helpers: {e}", level="DEBUG")

    def _reset_input_selectors(self):
        """Reset programme helpers and cancel Running-era timers (full Off cleanup)."""
        self._set_programme_helpers_default()

        # Cancel energy detection
        if self.energy_check_timer:
            self._safe_cancel_timer(self.energy_check_timer)
            self.energy_check_timer = None
        if self.unemptied_door_recheck_timer:
            self._safe_cancel_timer(self.unemptied_door_recheck_timer)
            self.unemptied_door_recheck_timer = None
        self.last_energy_value = None
        self.last_energy_time = None
        self.energy_stable_start_time = None
        self.last_high_energy_at = None
        self.energy_buffer = []

        if self.poll_timer:
            self._safe_cancel_timer(self.poll_timer)
            self.poll_timer = None
        if self.history_poll_timer:
            self._safe_cancel_timer(self.history_poll_timer)
            self.history_poll_timer = None

        if self.pause_timer:
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None

        # Cancel watchdog timers
        if self.running_watchdog_timer:
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
        if self.unemptied_watchdog_timer:
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None
        if self.emptied_watchdog_timer:
            self._safe_cancel_timer(self.emptied_watchdog_timer)
            self.emptied_watchdog_timer = None

    def _power_changed(self, entity, attr, old, new, kwargs):
        try:
            if new in ["unknown", "unavailable"]:
                self._handle_unavailable(entity, attr, old, new, kwargs)
                return
            watts = float(new or 0)
        except (ValueError, TypeError):
            self.log(f"Non-numeric power reading: {new}", level="WARNING")
            return

        # Plug is reporting numbers again - stand down the dead-plug watchdog and the
        # pending forced-Off grace.
        if self._plug_outage_push_timer:
            self._safe_cancel_timer(self._plug_outage_push_timer)
            self._plug_outage_push_timer = None
        if self._plug_outage_pushed:
            self._plug_outage_pushed = False
            self._push_mobile("Power plug is reporting again - washer monitoring resumed.")
        self._cancel_power_unavailable_grace()

        # FIX 1c: a fresh sample at/above start current corroborates a restored Running/Paused
        # whose clock came from a non-live source (see initialize()'s restore corroboration) -
        # the machine is demonstrably drawing, so drop the "uncorroborated" suppression, cancel
        # the pending reconcile, and let the real finish announce normally.
        if getattr(self, "restored_uncorroborated", False) and watts >= self.start_w:
            self.restored_uncorroborated = False
            if self._restore_reconcile_timer:
                self._safe_cancel_timer(self._restore_reconcile_timer)
                self._restore_reconcile_timer = None
            self.log(
                f"Restore corroborated by live power {watts:.0f}W (>= {self.start_w:.0f}W) - "
                f"clearing uncorroborated flag; cycle continues as normal Running",
                level="INFO",
            )

        current_state = self.get_state(self.state_entity)

        if current_state == "Running":
            self._record_power_reading(watts)
            if self.in_finishing_tail and watts > self._tail_pulse_reset_threshold_watts():
                self.last_tail_pulse_at = self._now_utc()
            # Track peak power and classify programme via heating signature.
            # The Miele WEA 035 heating element draws ~1800-2200W; any reading >1000W
            # unambiguously identifies a warm-water programme (Cotton, Eco, Synthetics).
            # Cold programmes (Wool, cold Quick) never exceed ~200W (motor + pump only).
            # Counting distinct heating BURSTS distinguishes Cotton 60°C (2 bursts) from
            # Cotton 40°C / Eco (1 burst). A burst ends when power drops back below 500W.
            if watts > self.max_power_seen:
                self.max_power_seen = watts
            if watts > 1000:
                if not self.in_heating_phase:
                    self.in_heating_phase = True
                    self.heating_phase_count += 1
                    if not self.observed_heating:
                        self.observed_heating = True
                    self.log(
                        f"Heating burst #{self.heating_phase_count} detected ({watts:.0f}W) "
                        f"- warm programme",
                        level="INFO",
                    )
            elif watts < 500 and self.in_heating_phase:
                self.in_heating_phase = False  # Heating phase ended (element off / rinsing)

        # Track significant power
        # During low-power detection, ignore brief spikes to prevent resetting the finish timer
        if watts >= self.significant_w:
            if self.low_power_start_time is None:
                # Not tracking low power - update normally
                self.last_significant_power_at = self._now_utc()
            else:
                # We're tracking low power - check if we should ignore this spike
                time_low = (self._now_utc() - self.low_power_start_time).total_seconds()
                poll_interval = int(self.args.get("poll_interval_s", 60))
                threshold_seconds = self.low_power_threshold * poll_interval
                
                # If we've accumulated significant low-power time (>=60% of threshold),
                # ignore brief spikes - we're likely in finish detection phase
                if time_low >= threshold_seconds * 0.6:
                    # Ignore spike - we're close to finish detection
                    self.log(f"Ignoring significant power spike ({watts:.1f}W) during finish detection (low power for {time_low:.0f}s)", level="DEBUG")
                else:
                    # Still early - update normally (might be inter-cycle pause)
                    self.last_significant_power_at = self._now_utc()

        # High power branch (start detection)
        if watts >= self.start_w:
            self.high_power_counter += 1
            self.low_power_counter = 0

            now = self._now_utc()
            fast_armed = (
                self.door_fast_start_armed_until is not None
                and now <= self.door_fast_start_armed_until
                and self.last_door_closed_trusted
            )
            effective_threshold = 2 if fast_armed else self.high_power_threshold

            if self.high_power_counter >= effective_threshold:
                if current_state == "Unemptied":
                    # False finish: we declared done but the machine is still running.
                    # Recover to Running so the UI shows correct state and we can detect real finish.
                    self._recover_from_false_unemptied(watts)
                    return
                if current_state == "Off":
                    self._confirm_running(kwargs={})
                elif current_state == "Emptied":
                    # User closed door and started a new cycle without us seeing Off (e.g. brief door events).
                    # Treat like Off -> Running so we get a new start_time after last door close.
                    self._confirm_running(kwargs={})
                elif current_state == "Running":
                    # If HA cycle_start_time is before last_off_at, the displayed start is stale (e.g. missed Off).
                    # Reset cycle clock while staying in Running. Do NOT use "run > 130 min" here - long ECO/Bomuld
                    # cycles legitimately exceed 130 min with high power during spins; that caused log spam and
                    # _confirm_running was a no-op while already Running anyway.
                    try:
                        last_off_str = self.get_state(self.state_entity, attribute="last_off_at")
                        start_str = self.get_state(self.state_entity, attribute="cycle_start_time")
                        last_off = _parse_utc(last_off_str) if last_off_str else None
                        start_dt = _parse_utc(start_str) if start_str else None
                        start_before_off = (
                            last_off and start_dt and start_dt < last_off
                            and (last_off - start_dt).total_seconds() >= self.pause_window_minutes * 60
                        )
                    except (TypeError, ValueError, AttributeError):
                        start_before_off = False
                    if start_before_off:
                        self._begin_running_cycle(
                            f"Running: reset cycle clock (cycle_start before last Off; was {self._strftime_local(self.start_time)})",
                        )
                elif current_state == "Paused":
                    self.log(f"Power high while Paused ({watts:.1f}W)", level="DEBUG")
        else:
            self.high_power_counter = 0

        # Emptied + 0W: machine is fully off - no need to wait for door close or watchdog timer.
        if current_state == "Emptied" and watts <= 0:
            self.log("Emptied: power is 0W - machine fully off, transitioning to Off", level="INFO")
            self._safe_cancel_timer(self.emptied_watchdog_timer)
            self.emptied_watchdog_timer = None
            self._transition_to_off("Emptied: 0W - machine off")
            return

        # Low power branch (finish detection)
        # Use time-based approach: track how long power has been low
        # Allow brief spikes without resetting - use 80% threshold for robustness
        if current_state == "Running":
            if watts <= self.stop_w:
                # Power is low - start or continue tracking
                if self.low_power_start_time is None:
                    self.low_power_start_time = self._now_utc()
                    self.low_power_counter = 1
                else:
                    self.low_power_counter += 1
                
                # Check if we've had low power long enough (with tolerance for brief spikes)
                # Require at least 80% of readings to be low over the threshold period
                poll_interval = int(self.args.get("poll_interval_s", 60))
                threshold_seconds = self.low_power_threshold * poll_interval
                time_low = (self._now_utc() - self.low_power_start_time).total_seconds()
                
                if time_low >= threshold_seconds * 0.8:  # 80% of threshold time
                    # Check if majority of readings were low
                    if self.low_power_counter >= int(self.low_power_threshold * 0.8):
                        self._confirm_finished(kwargs={})
            else:
                # Power is above threshold - check if we should reset
                if self.low_power_start_time is not None:
                    # We've been tracking low power, but now it's high
                    # Allow up to 20% of readings to be high (tolerance for brief spikes)
                    # If we've accumulated enough low readings, tolerate occasional spikes
                    poll_interval = int(self.args.get("poll_interval_s", 60))
                    threshold_seconds = self.low_power_threshold * poll_interval
                    time_low = (self._now_utc() - self.low_power_start_time).total_seconds()
                    
                    # Calculate expected readings in this period
                    expected_readings = max(1, int(time_low / poll_interval))
                    # If we have at least 80% low readings, this is just a spike - don't reset yet
                    if self.low_power_counter >= int(expected_readings * 0.8):
                        self.log(f"Power spike to {watts:.1f}W during low-power period (tolerated, {self.low_power_counter}/{expected_readings} low)", level="DEBUG")
                    else:
                        # Too many high readings - reset tracking
                        self.log(f"Power recovered to {watts:.1f}W after {time_low:.0f}s - reset (only {self.low_power_counter}/{expected_readings} low)", level="DEBUG")
                        self.low_power_counter = 0
                        self.low_power_start_time = None
                else:
                    self.low_power_counter = 0

    def _poll_power(self, kwargs):
        """Conditional polling"""
        current_state = self.get_state(self.state_entity)

        if current_state not in ("Running", "Paused", "Emptied", "Unemptied"):
            if self.poll_timer:
                self._safe_cancel_timer(self.poll_timer)
                self.poll_timer = None
            return

        # Missed or ignored listen_state (e.g. open after old=unknown post-restart): door can be open
        # while sensor.washer_state still says Running. Reconcile like Unemptied door-recheck.
        if current_state == "Running" and self._door_is_physically_open():
            self.log(
                "Poll: door reports open while state Running (missed/ignored door event) - applying door-open handling",
                level="INFO",
            )
            self._handle_door_opened("Running")
        elif current_state == "Paused" and not self._door_is_physically_open():
            # Recover if the door-close event was missed or if a rapid close was
            # blocked by cooling right after entering Paused.
            self.log(
                "Poll: state Paused but door is closed - applying pause-exit handling",
                level="INFO",
            )
            current_power = self._get_current_power()
            if current_power >= self.start_w:
                self._transition_to_running_from_pause(force=True)
            else:
                self._evaluate_pause_exit(force=True)

        current_power_state = self.get_state(self.power_sensor)
        if current_power_state in ["unknown", "unavailable"]:
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        try:
            watts = float(current_power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        self._power_changed(self.power_sensor, None, None, watts, {})

        poll_interval = int(self.args.get("poll_interval_s", 60))
        self.poll_timer = self.run_in(self._poll_power, poll_interval)

    def _begin_running_cycle(self, log_message="State -> Running"):
        """Reset per-cycle state and push Running attributes.

        Used when entering Running from Off/Emptied, or when fixing stale cycle_start_time while
        the entity already shows Running (see _power_changed start_before_off path).
        """
        self.last_state_change = self._now_utc()
        self.state = "Running"
        self.in_finishing_tail = False
        self.in_finishing_tail_entered_at = None
        self.last_tail_pulse_at = None
        # Reset all per-cycle counters so stale data from a previous cycle never bleeds through.
        self.max_power_seen = 0.0
        self.observed_heating = False
        self.in_heating_phase = False
        self.heating_phase_count = 0
        self.finish_confirmed = False
        self.energy_stable_start_time = None
        self.last_high_energy_at = None
        self._zero_power_since = None
        # A freshly (re)started cycle is live by definition - never carry a prior restore's
        # uncorroborated suppression into it (FIX 1).
        self.restored_uncorroborated = False
        self.expected_dur_at_start = None
        self._guard_bar_class = None
        self._live_class_key = None
        self._live_class_since = None
        self._delay_plateau_start = None
        self._delayed_start_trimmed = False
        self._delay_waiting = False
        self._delayed_start_lead_idle_min = None
        # Vibration telemetry is Running-scoped (see initialize()) - reset here like the other
        # per-cycle counters above. _vibration_events is NOT reset (it survives cycle boundaries
        # for the post-save unload window).
        self.vibration_pulse_count = 0
        self.vibration_on_seconds = 0.0
        self.first_vibration_at = None
        self.last_vibration_at = None
        self._vibration_on_started = None
        # Attribution anchor: the moment the human LOADED the machine, not when the motor
        # starts - a Miele delayed start can defer the motor by hours (see DELAYED START in
        # washer.yaml), which would otherwise point ActorAttribution at whoever was home hours
        # later. Reuses the same trust/freshness bar as the start_time door-close clamp below
        # (~3705-3714): only a recent (<=12h), trusted door close that isn't stale from a
        # PREVIOUS cycle (before last Off) counts; otherwise fall back to now.
        attribution_anchor = self._now_utc()
        if self.last_door_closed_trusted and self.last_door_closed_at:
            door_age_s = (attribution_anchor - self.last_door_closed_at).total_seconds()
            last_off_at = None
            try:
                last_off_str = self.get_state(self.state_entity, attribute="last_off_at")
                if last_off_str:
                    last_off_at = _parse_utc(last_off_str)
            except (TypeError, ValueError, AttributeError):
                last_off_at = None
            not_before_off = last_off_at is None or self.last_door_closed_at >= last_off_at
            if 0 <= door_age_s <= 12 * 3600 and not_before_off:
                attribution_anchor = self.last_door_closed_at
        self._cycle_actor = self._attribute("washer_start", at=attribution_anchor)
        self.start_time = self._now_utc()
        self._start_time_source = "live"  # directly observed the start - see _START_TIME_SOURCE_RANK
        self._entity_recreated_at = None  # observed a real start; any earlier recreation stamp is now irrelevant
        # Start time cannot be before the last door close (except in first 10 min AddLoad).
        if (
            self.last_door_closed_trusted
            and self.last_door_closed_at
            and self.start_time < self.last_door_closed_at
        ):
            self.log(
                f"Clamping start_time to last door close {self._format_local(self.last_door_closed_at)} (was {self._format_local(self.start_time)})",
                level="INFO",
            )
            self.start_time = self.last_door_closed_at
        # Never show a start time before we last went Off (second cycle must start after Off).
        try:
            last_off_str = self.get_state(self.state_entity, attribute="last_off_at")
            if last_off_str:
                last_off = _parse_utc(last_off_str)
                if last_off and self.start_time < last_off:
                    gap = (last_off - self.start_time).total_seconds()
                    if gap >= self.pause_window_minutes * 60:
                        self.log(
                            f"Clamping start_time to after last Off {self._format_local(last_off)} (was {self._format_local(self.start_time)})",
                            level="INFO",
                        )
                        self.start_time = last_off
        except (TypeError, ValueError, AttributeError):
            pass
        self.notification_sent = False
        # Fresh identity for this NEW cycle, paired with notification_sent=False just above -
        # both reset together whenever a cycle actually (re)starts live. On restore, by
        # contrast, notification_sent is decided by self._start_time_source == "durable_store",
        # not by comparing cycle_id values - see _finalize_restored_cycle_identity; cycle_id
        # there is only carried alongside that same decision, never itself the discriminator.
        self._cycle_id = str(uuid.uuid4())
        self.door_opened_during_cycle = False
        self.low_power_counter = 0
        self.low_power_start_time = None
        self.power_readings = []

        try:
            energy = self.get_state(self.energy_sensor)
            if energy is not None and energy not in ["unknown", "unavailable"]:
                self.energy_start = float(energy)
        except (ValueError, TypeError):
            self.energy_start = None

        # Settled per-cycle cost: fresh accumulator + baseline for this cycle (see _check_energy_finish).
        self._session_cost_kr = 0.0
        self._cost_prev_energy_kwh = self.energy_start

        # Set state and initial attributes immediately so UI shows this cycle's start time.
        self.detected_programme = "unknown"
        self.detected_temperature = None
        profile = self._get_profile("unknown")
        attrs = {
            "detected_programme": "unknown",
            "detected_temperature": "",
            "programme_label": profile.get("label", "Unknown"),
            "heating_bursts": self.heating_phase_count,
            "max_power_w": round(self.max_power_seen, 0),
            "cycle_start_time": self._format_utc(self.start_time),
            "cycle_start_time_local": self._format_local(self.start_time),
            "started_at_display": self.start_time.astimezone(self._local_tz()).strftime("%H:%M"),
            "elapsed_minutes": 0,
            "progress_pct": 0,
            "estimated_remaining_min": profile["duration_min"],
            "estimated_end_time": (self.start_time + timedelta(minutes=profile["duration_min"])).astimezone(self._local_tz()).strftime("%H:%M"),
            "programme_duration_min": profile["duration_min"],
            "delayed_start_trimmed": bool(self._delayed_start_trimmed),
            "delayed_start_waiting": bool(self._delay_waiting),
            "session_cost_kr": round(self._session_cost_kr, 2),
        }
        # Use "" not None - AppDaemon 4.5.13 drops attributes equal to None/False/0 (see
        # smart_cooling.py's _publish() for details); "unknown" mirrors detected_programme's
        # own placeholder above.
        attrs["started_by"] = self._cycle_actor.get("person") or ""
        attrs["started_by_method"] = self._cycle_actor.get("method") or "unknown"
        if self.energy_start is not None:
            attrs["energy_at_start"] = self.energy_start
        if self.last_door_closed_at:
            attrs["last_door_closed_at"] = self._format_local(self.last_door_closed_at)
        attrs["last_door_closed_trusted"] = bool(self.last_door_closed_trusted)
        try:
            last_off = self.get_state(self.state_entity, attribute="last_off_at")
            if last_off:
                attrs["last_off_at"] = last_off
        except Exception:
            pass
        confirmed = False
        confirmed_by = ""
        prog = "unknown"
        if self.confirm_entity:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    if prog and prog != "unknown":
                        confirmed = True
                        confirmed_by = self.get_state(self.state_entity, attribute="programme_confirmed_by") or ""
            except Exception:
                pass
        self.programme_confirmed_by_user = confirmed
        self.confirmed_by_username = confirmed_by or None
        attrs["programme_confirmed_by_user"] = confirmed
        attrs["programme_confirmed_by"] = confirmed_by
        if confirmed and self.confirm_entity:
            try:
                temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
                dur = self._get_programme_duration(prog, temp, use_learned=False)
                if dur:
                    self.expected_dur_at_start = float(dur)
                    self._guard_bar_class = (prog, temp)
                    attrs["expected_dur_at_start"] = self.expected_dur_at_start
                    attrs["expected_dur_key"] = self._guard_bar_key_str()
                    self.log(f"Expected duration set from confirmed programme: {self.expected_dur_at_start:.0f} min", level="DEBUG")
            except Exception:
                pass
        if self.expected_dur_at_start is None:
            attrs["expected_dur_at_start"] = ""
            attrs["expected_dur_key"] = ""
        # elapsed_minutes/progress_pct/heating_bursts/max_power_w are always 0/0.0 at cycle start
        # (just reset); last_door_closed_trusted/programme_confirmed_by_user/delayed_start_trimmed/
        # delayed_start_waiting are commonly False too (no trusted door-close yet, Auto-mode default,
        # fresh cycle) -- AppDaemon 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish()
        # for details.
        self._set_state_entity( state="Running", attributes=attrs, replace=True)

        if not self.poll_timer:
            poll_interval = int(self.args.get("poll_interval_s", 60))
            self.poll_timer = self.run_in(self._poll_power, poll_interval)
        if not self.history_poll_timer:
            interval = int(self.args.get("history_check_interval_s", 300))
            self.history_poll_timer = self.run_in(self._periodic_check_power_history, interval)

        self._safe_cancel_timer(self.running_watchdog_timer)
        self.running_watchdog_timer = self.run_in(
            self._running_watchdog_timeout,
            int(self.max_running_hours * 3600)
        )

        if self.use_energy_detection:
            self._start_energy_detection()

        if log_message:
            self.log(log_message, level="INFO")

    def _confirm_running(self, kwargs):
        current_power_state = self.get_state(self.power_sensor)
        if current_power_state in ["unknown", "unavailable"]:
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        try:
            watts_confirm = float(current_power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        if watts_confirm >= self.start_w:
            if self._should_change_state("Running", force=True):
                self._begin_running_cycle("State -> Running")
                self.door_fast_start_armed_until = None

    def _confirm_finished(self, kwargs):
        """Confirm the cycle has finished - power dropped, door still closed.
        Skipped when energy-based detection is active (it's more reliable for
        machines with post-cycle pump spikes like the Miele)."""
        current_state = self.get_state(self.state_entity)

        if current_state != "Running":
            return

        if getattr(self, 'energy_check_timer', None) is not None:
            self.log("Power-based finish skipped - energy detection is active", level="DEBUG")
            return

        current_power_state = self.get_state(self.power_sensor)
        if current_power_state in ["unknown", "unavailable"]:
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        try:
            watts = float(current_power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        # Check time since significant power
        # Since we've already detected 5+ minutes of low power, we only need a short confirmation
        # that power is still low and there's been no recent significant activity
        time_since_high = float("inf")
        if self.last_significant_power_at:
            time_since_high = (self._now_utc() - self.last_significant_power_at).total_seconds()

        # Reduced requirement: if we've detected low power for threshold period,
        # we only need 1 minute (2 poll intervals) without significant power as confirmation
        # This prevents the double-delay issue where we wait 5 min + another 5 min
        confirmation_time = min(self.no_recent_high_s, 60)  # Max 1 minute confirmation
        
        if watts <= self.stop_w and time_since_high >= confirmation_time:
            run_min = self._get_run_duration_minutes()
            prog, temp = self._classify_programme() if self.start_time else ("unknown", None)
            guard_dur = self._get_guard_duration(tick_prog=prog, tick_temp=temp, tick_class=(prog, temp))
            if self._meets_finish_time_guards(run_min, guard_dur or 0) and self._is_valid_completed_cycle():
                self.finish_confirmed = True
                self.log("Finish confirmed (power-based detection)", level="INFO")
                self._transition_to_unemptied()
            elif not self._meets_finish_time_guards(run_min, guard_dur or 0):
                self.log(f"Power-based: finish time guards not met (run {run_min:.0f}min) - blocking", level="DEBUG")
            else:
                self.log(f"Cycle incomplete - waiting (time since high: {time_since_high:.0f}s, need {confirmation_time}s)", level="DEBUG")
        elif watts <= self.stop_w:
            self.log(f"Power low but waiting for confirmation (time since high: {time_since_high:.0f}s, need {confirmation_time}s)", level="DEBUG")

    def _cancel_power_unavailable_grace(self):
        """Power/state readings are valid again; cancel the pending forced-Off transition."""
        self._safe_cancel_timer(self.power_unavailable_off_timer)
        self.power_unavailable_off_timer = None

    def _handle_unavailable(self, entity, attribute, old, new, kwargs):
        """Handle entity becoming unavailable - do not force-wipe an in-progress cycle on a brief
        dropout (HA restart or ESPHome OTA flash routinely drop the plug for well under the grace
        period - 2026-07-17 log investigation showed dishwasher_monitor.py absorbs the same outages
        with zero false transitions). Wait for the outage to persist before forcing Off; a lasting
        plug outage separately pages the phone (_begin_plug_outage_grace).
        The already-running guard below also dedups the double-invocation that used to log twice
        11 ms apart: the 'unavailable' listen_state AND the unavailable branch of _power_changed
        both call this."""
        if entity == self.power_sensor:
            self._begin_plug_outage_grace()
        if self.power_unavailable_off_timer and self.timer_running(self.power_unavailable_off_timer):
            return
        self.power_unavailable_off_timer = self.run_in(
            self._power_unavailable_off_timeout,
            self.power_unavailable_off_after_seconds,
        )
        self.log(
            f"{entity} unavailable ({new}); waiting {self.power_unavailable_off_after_seconds}s before forcing Off "
            f"(short dropouts - HA restart / plug OTA - are ignored)",
            level="WARNING",
        )

    def _power_unavailable_off_timeout(self, kwargs):
        self.power_unavailable_off_timer = None
        ps = self.get_state(self.power_sensor)
        if ps not in ("unknown", "unavailable", None):
            self.log(f"Power sensor recovered before the {self.power_unavailable_off_after_seconds}s grace expired", level="INFO")
            return
        self._transition_to_off(f"Power sensor unavailable >= {self.power_unavailable_off_after_seconds}s", force=True)

    def _begin_plug_outage_grace(self):
        """Short plug dropouts are routine; only a lasting outage pages the phone."""
        if self._plug_outage_pushed:
            return
        if self._plug_outage_push_timer and self.timer_running(self._plug_outage_push_timer):
            return
        self._plug_outage_push_timer = self.run_in(
            self._plug_outage_push_timeout, self.plug_outage_push_after_seconds
        )

    def _plug_outage_push_timeout(self, kwargs):
        self._plug_outage_push_timer = None
        if self.get_state(self.power_sensor) not in ("unknown", "unavailable", None):
            return
        self._plug_outage_pushed = True
        self._push_mobile(
            f"Power plug stopped reporting (unavailable >= {self.plug_outage_push_after_seconds}s) - "
            f"cycle monitoring is blind and the washer just looks Off. Check the plug/WiFi."
        )

    def _push_mobile(self, message):
        """Page the phone (plug outage / recovery) - same pattern as gw2000a_watchdog."""
        try:
            notifier = self.get_app("MobileNotifier")
            if notifier is None:
                self.log("MobileNotifier app not found - cannot push", level="WARNING")
                return
            self.create_task(notifier.notify(title="Washer", message=message, target=self.notify_target))
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")

    def analyze_recent_cycles(self, hours_back=48):
        """
        Analyze recent washer cycles using Home Assistant history.
        Can be called via service: appdaemon.call_service('washer_monitor', 'analyze_recent_cycles', {'hours_back': 48})
        """
        try:
            from datetime import timedelta
            end_time = self._now_utc()
            start_time = end_time - timedelta(hours=hours_back)
            
            self.log(f"Analyzing cycles from {start_time} to {end_time}", level="INFO")
            
            # Get history for all relevant entities
            energy_history = self._flatten_history(
                self.get_history(
                    entity_id=self.energy_sensor,
                    start_time=start_time,
                    end_time=end_time
                ), self.energy_sensor
            )
            door_history = self._flatten_history(
                self.get_history(
                    entity_id=self.door_sensor,
                    start_time=start_time,
                    end_time=end_time
                ), self.door_sensor
            )
            power_history = self._flatten_history(
                self.get_history(
                    entity_id=self.power_sensor,
                    start_time=start_time,
                    end_time=end_time
                ), self.power_sensor
            )
            state_history = self._flatten_history(
                self.get_history(
                    entity_id=self.state_entity,
                    start_time=start_time,
                    end_time=end_time
                ), self.state_entity
            )
            
            # Parse and identify cycles
            cycles = self._identify_cycles_from_history(
                energy_history, door_history, power_history, state_history
            )
            
            # Log analysis
            self._log_cycle_analysis(cycles)
            
            return cycles
            
        except Exception as e:
            self.log(f"Error analyzing cycles: {e}", level="ERROR")
            import traceback
            self.log(traceback.format_exc(), level="ERROR")
            return []

    def _identify_cycles_from_history(self, energy_hist, door_hist, power_hist, state_hist):
        """Identify individual cycles from history data. See washer_history.identify_cycles."""
        return whist.identify_cycles(
            energy_hist, door_hist, power_hist, state_hist,
            self.start_w, self.stop_w, self.high_power_threshold, self.low_power_threshold,
        )

    def _classify_programme(self, energy_signature_only: bool = False):
        """Classify the running programme from power signature, energy, and runtime.

        Returns (programme, temperature) tuple.  Temperature is only set for
        'bomuld' where it can be inferred from energy gates or user selection.

        User confirmation is authoritative - never overridden by classification.
        If the user has selected a programme in the dropdown, we use that.

        When energy_signature_only=True, skip user/selector branches so the result is
        purely from energy/heating/history - used for predicted_* attributes while the
        user keeps Auto (unconfirmed).

        Cold programmes (heating element never observed):
          run < 25 min           -> ("ekspres", None)
          energy < 0.28 kWh     -> ("uld", None)
          otherwise              -> ("bomuld", "20°C")

        Warm programmes (heating observed, descending energy order):
          energy > 1.35 kWh     -> ("bomuld", "90°C")
          energy > 0.85 kWh     -> ("bomuld", "60°C")
          run > 140 & e >= 0.55 -> ("bomuld", "40°C")
          run > 130 min         -> ("eco", None)
          otherwise              -> ("eco", None) or user prior

        Returns ("unknown", None) until >= 10 min of runtime has elapsed.
        """
        if not self.start_time:
            return ("unknown", None)

        run_min = (self._now_utc() - self.start_time).total_seconds() / 60
        energy = self._get_energy_used()

        if run_min < 10:
            return ("unknown", None)

        # User confirmation is authoritative - never override with classification.
        # Only when programme_confirmed_by_user (set by _on_confirm_changed). The selector alone is not
        # enough: we used to mirror prediction into the dropdown, which must not lock classification.
        if not energy_signature_only and self.confirm_entity and self.programme_confirmed_by_user:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    if prog and prog != "unknown":
                        temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
                        return (prog, temp)
            except Exception:
                pass

        # Energy implies heating: >0.5 kWh in <35 min cannot be cold (Ekspress max 0.4, Uld max 0.28).
        # Wrong start_time (door opened during soak) can make observed_heating=False; energy reveals truth.
        # Only when no user confirmation - never override user's Ekspress.
        if not self.observed_heating and energy > 0.5 and run_min < 35:
            self.log(
                f"Inferring heating from energy: {energy:.2f}kWh in {run_min:.0f}min - treating as warm (wrong start_time)",
                level="INFO",
            )
            # Fall through to warm branch
        elif not self.observed_heating:
            # --- Cold programmes (no heating element ever fired) ---
            # Ekspress: run < 25 min, no heating, max ~0.4 kWh. If energy > 0.45 kWh in <25 min,
            # we have wrong start_time - never classify as Ekspress; use bomuld 20°C (long guard).
            if run_min < 25:
                if energy > 0.45:
                    self.log(
                        f"Ekspress blocked: energy {energy:.2f}kWh in {run_min:.0f}min - likely wrong start_time (long programme)",
                        level="INFO",
                    )
                    return ("bomuld", "20°C")
                return ("ekspres", None)
            uld_profile = self._get_profile("uld")
            if energy < uld_profile.get("max_energy_kwh", 0.28):
                return ("uld", None)
            return ("bomuld", "20°C")

        # --- Warm programmes (heating observed) - descending energy gates ---
        if energy > 1.35:
            return ("bomuld", "90°C")
        if energy > 0.85:
            return ("bomuld", "60°C")
        if run_min > 140 and energy >= 0.55:
            return ("bomuld", "40°C")
        if run_min > 130:
            return ("eco", None)

        # Ambiguous warm mid-cycle: use user's programme + temperature as prior (only if they confirmed).
        if not energy_signature_only and self.confirm_entity and self.programme_confirmed_by_user:
            try:
                prog_label = self.get_state(self.confirm_entity)
                if prog_label and prog_label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog_key = self._LABEL_TO_KEY.get(prog_label)
                    if prog_key and prog_key != "eco":
                        temp = self._read_temperature_selector()
                        if temp and not self._programme_has_temperature(prog_key):
                            temp = None
                        return (prog_key, temp)
            except Exception:
                pass
        # No user prior: use power-pattern match to historical confirmed cycles
        hist = self._classify_from_history(run_min, energy, self.heating_phase_count)
        if hist:
            self.log(
                f"Pattern match from history -> {hist} (rate {energy / run_min:.4f} kWh/min, {self.heating_phase_count} heating bursts)",
                level="DEBUG",
            )
            return hist
        return ("eco", None)

    def _read_temperature_selector(self):
        """Read the temperature input_select and return a normalized value like '40°C', 'cold', or None."""
        if not self.temperature_entity:
            return None
        try:
            val = self.get_state(self.temperature_entity)
            if val and val not in ("unknown", "unavailable"):
                return wcls.normalize_selector_temperature(val)
        except Exception:
            pass
        return None

    # JSON storage codec for temperature: '40°C' <-> '40', 'cold' passes through.
    _temp_for_storage = staticmethod(wcls.temp_for_storage)
    _temp_from_storage = staticmethod(wcls.temp_from_storage)

    def _classify_from_history(self, run_min: float, energy_used: float, heating_bursts: int):
        """Use power-pattern match to historical confirmed cycles when ambiguous.

        Centroids are keyed by "prog|temp" strings.
        Returns (programme, temperature) tuple or None.
        """
        best_key = wcls.classify_from_history(
            self._history_centroids, run_min, energy_used, heating_bursts
        )
        if best_key is None:
            return None
        parts = best_key.split("|", 1)
        prog = parts[0]
        temp = parts[1] if len(parts) > 1 and parts[1] else None
        if temp and not self._programme_has_temperature(prog):
            temp = None
        return (prog, temp)

    def _programme_max_duration_minutes(self, classification=None):
        """Hard maximum runtime for the classified programme (duration tripwire)."""
        prog, temp = classification or self._classify_programme()
        profile = self._get_profile(prog, temp)
        if profile and "max_dur_min" in profile:
            return profile["max_dur_min"]
        return int(self.max_running_hours * 60)

    def _note_live_classification(self, prog, temp):
        """Track how long the live classification has continuously held one programme+temperature.

        Called once per _check_energy_finish tick. Feeds the stability requirement in
        _update_guard_bar: only a classification that has stopped flip-flopping may lower
        the finish-guard bar (the 2026-08-11 tape flapped eco<->bomuld60 every 30 s at the
        0.85 kWh gate). In-memory only - after an app restart the streak restarts, which
        just delays a possible lowering by guard_reclass_stable_minutes (conservative)."""
        if not prog or prog == "unknown":
            self._live_class_key = None
            self._live_class_since = None
            return
        key = f"{prog}|{temp or ''}"
        if key != self._live_class_key:
            self._live_class_key = key
            self._live_class_since = self._now_utc()

    def _live_class_stable_minutes(self, prog, temp) -> float:
        """Minutes the live classification has continuously been (prog, temp); 0 when it just changed."""
        key = f"{prog}|{temp or ''}"
        if self._live_class_key != key or self._live_class_since is None:
            return 0.0
        return (self._now_utc() - self._live_class_since).total_seconds() / 60

    def _guard_bar_key_str(self) -> str:
        """Serialize _guard_bar_class for entity-attr persistence ("prog|temp", temp may be empty)."""
        if not self._guard_bar_class:
            return ""
        prog, temp = self._guard_bar_class
        return f"{prog}|{temp or ''}"

    @staticmethod
    def _parse_guard_bar_key(s):
        """Parse a persisted "prog|temp" bar key back to (prog, temp) or None."""
        if not s or not isinstance(s, str) or s in ("unknown", "unavailable"):
            return None
        parts = s.split("|", 1)
        prog = parts[0].strip()
        if not prog or prog == "unknown":
            return None
        temp = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        return (prog, temp)

    def _update_guard_bar(self, new_prog, new_temp):
        """Freeze/raise/lower expected_dur_at_start from this tick's live classification.

        The decision itself is pure (wcls.resolve_guard_bar - see its docstring for the
        2026-08-11 incident rationale); this method feeds it live durations, the bar
        programme's max plausible energy, cumulative cycle energy and the stability streak,
        then applies + logs the result. Runs on every Running energy tick, before the
        finish paths of the same tick consult _get_guard_duration."""
        if not new_prog or new_prog == "unknown":
            return
        live_dur = self._get_programme_duration(new_prog, new_temp, use_learned=False)
        if not live_dur:
            return
        bar_profile = self._get_profile(*self._guard_bar_class) if self._guard_bar_class else None
        live_profile = self._get_profile(new_prog, new_temp)
        bar_max_energy = None
        if bar_profile:
            bar_max_energy = bar_profile.get("max_valid_energy_kwh") or bar_profile.get("max_energy_kwh")
        live_max_energy = None
        if live_profile:
            live_max_energy = live_profile.get("max_valid_energy_kwh") or live_profile.get("max_energy_kwh")
        energy_used = self._get_energy_used()
        new_bar, action = wcls.resolve_guard_bar(
            bar_min=self.expected_dur_at_start,
            bar_max_energy_kwh=bar_max_energy,
            live_dur_min=live_dur,
            live_max_energy_kwh=live_max_energy,
            live_stable_minutes=self._live_class_stable_minutes(new_prog, new_temp),
            energy_used_kwh=energy_used,
            lower_stable_minutes=self.guard_reclass_stable_minutes,
            disproof_margin=self.guard_energy_disproof_margin,
        )
        if action is None:
            return
        old = self.expected_dur_at_start
        self.expected_dur_at_start = new_bar
        self._guard_bar_class = (new_prog, new_temp)
        temp_str = f" {new_temp}" if new_temp else ""
        if action == "freeze":
            self.log(f"Frozen expected_dur_at_start: {new_bar:.0f} min", level="DEBUG")
        elif action == "raise":
            self.log(
                f"Raised expected_dur_at_start: {old:.0f} -> {new_bar:.0f} min "
                f"(classified '{new_prog}'{self._log_safe(temp_str)})",
                level="INFO",
            )
        elif action == "lower":
            self.log(
                f"Lowered expected_dur_at_start: {old:.0f} -> {new_bar:.0f} min "
                f"('{new_prog}'{self._log_safe(temp_str)} stable "
                f"{self._live_class_stable_minutes(new_prog, new_temp):.0f}min, energy "
                f"{energy_used:.2f}kWh disproves previous programme, max {bar_max_energy or 0:.2f}kWh)",
                level="INFO",
            )
        else:  # rekey - bar value unchanged, programme key adopted (e.g. after restart restore)
            self.log(
                f"Guard bar programme adopted from live classification: '{new_prog}'{self._log_safe(temp_str)} "
                f"({new_bar:.0f} min)",
                level="DEBUG",
            )

    def _get_guard_duration(self, tick_prog=None, tick_temp=None, tick_class=None):
        """Best duration for 85% guards: prefer user-confirmed selector, else the guard bar
        (expected_dur_at_start - seeded at first classification, then evidence-following via
        _update_guard_bar), else this tick's classification, else programme max.
        Only trust confirm_entity when programme_confirmed_by_user is True; otherwise the selector may hold
        an auto-filled prediction and must not drive finish guards."""
        if self.programme_confirmed_by_user and self.confirm_entity:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
                    if prog and prog != "unknown":
                        d = self._get_programme_duration(prog, temp, use_learned=False)
                        if d:
                            return d
            except Exception:
                pass
        if self.expected_dur_at_start is not None:
            return self.expected_dur_at_start
        if tick_prog and tick_prog != "unknown":
            d = self._get_programme_duration(tick_prog, tick_temp, use_learned=False)
            if d:
                return d
        return self._programme_max_duration_minutes(classification=tick_class)

    def _get_finish_min_run_minutes(self):
        """Minimum run minutes before we may declare cycle done (avoids false finish when guard_dur is wrong).
        Use warm floor when we've seen heating, or when user has confirmed a programme that heats (so we don't
        fire early before the first heating burst in a long warm programme)."""
        if self.observed_heating:
            return self.finish_min_run_minutes_warm
        if self.programme_confirmed_by_user and self.confirm_entity:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
                    if prog and prog != "unknown":
                        profile = self._get_profile(prog, temp)
                        if profile and profile.get("heats"):
                            return self.finish_min_run_minutes_warm
            except Exception:
                pass
        return self.finish_min_run_minutes_cold

    def _meets_finish_time_guards(self, run_min: float, guard_dur: float) -> bool:
        """True only if we're past the fraction of expected AND past absolute min runtime. Reduces false announcements."""
        min_run = self._get_finish_min_run_minutes()
        if not guard_dur:
            return run_min >= min_run
        # When we use the warm floor, don't trust a guard_dur below it for the percentage check (avoids wrong classification).
        effective_guard = max(guard_dur, min_run) if min_run == self.finish_min_run_minutes_warm else guard_dur
        pct_ok = run_min >= effective_guard * self.finish_guard_fraction
        min_ok = run_min >= min_run
        return pct_ok and min_ok

    def _effective_stable_minutes(self, classification=None):
        """Energy-stability window appropriate for the detected programme.
        Only use confirm_entity when programme_confirmed_by_user is True (avoids using
        auto-filled prediction for stable window)."""
        prog, temp = None, None
        if self.programme_confirmed_by_user and self.confirm_entity:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    temp = self._read_temperature_selector() if prog and self._programme_has_temperature(prog) else None
            except Exception:
                pass
        if not prog or prog == "unknown":
            prog, temp = classification or self._classify_programme()
        profile = self._get_profile(prog, temp)
        if profile and "stable_min" in profile:
            return profile["stable_min"]
        return self.energy_stable_minutes

    def _estimated_remaining_minutes(self):
        """Estimate minutes remaining based on programme profile and elapsed time."""
        if not self.start_time:
            return None
        prog, temp = self._classify_programme()
        if prog == "unknown":
            return None
        effective_dur = self._get_programme_duration(prog, temp)
        elapsed_min = (self._now_utc() - self.start_time).total_seconds() / 60
        return max(0, round(effective_dur - elapsed_min))

    # =========================================================================
    # Programme feedback & learning
    # =========================================================================

    # Map from the human-readable input_select labels back to programme keys.
    # Temperature is always a separate dimension read from temperature_entity.
    #
    # HA contract: confirm_entity (Washer Confirmed Programme) must have options
    # programme name only: Auto (unconfirmed), Ekspres, Uld, Bomuld, Finvask,
    # Strygelet, ECO. Temperature and spin are separate helpers (temperature_entity,
    # spin_entity). Align HA helpers via MCP (ha_config_set_helper) or UI so the
    # dropdowns match; the app can call input_select.set_options at startup to
    # re-apply programme options if the helper was reverted.
    _LABEL_TO_KEY = dict(wp.LABEL_TO_KEY)

    def _get_programme_duration(self, prog: str, temperature=None, use_learned: bool = True) -> int:
        """Return the effective expected duration for a programme (minutes).

        Uses learned average from confirmed historical cycles when use_learned=True.
        For guards (85% rule), use use_learned=False - learned can be polluted by
        false finishes and must not shorten the guard.
        For 'bomuld', temperature selects the right sub-profile.
        """
        if temperature and not self._programme_has_temperature(prog):
            temperature = None
        profile = self._get_profile(prog, temperature)
        manual = profile.get("duration_min", 180)
        if not use_learned:
            return manual
        learn_key = f"{prog}|{temperature}" if temperature else prog
        return wp.blend_learned_duration(manual, self._learned_durations.get(learn_key))

    def _load_and_apply_feedback(self):
        """Load washer_feedback.json and apply learned programme data.

        Handles both v1 (compound keys like bomuld_40) and v2 (programme + temperature)
        formats transparently. Called once at startup.
        """
        import json
        import os

        self._learned_durations = {}
        self._history_centroids = {}

        path = self.feedback_file
        if not os.path.exists(path):
            # Fallback: path next to this app (in case config path is from different root, e.g. /data vs /conf)
            fallback = os.path.join(os.path.dirname(__file__), "washer_feedback.json")
            if os.path.exists(fallback):
                path = fallback
                self.feedback_file = path
                self.log(f"Using feedback file next to app: {path}", level="INFO")
            else:
                self.log(f"No feedback file found at {self.feedback_file} or {fallback} - using manual programme profiles", level="INFO")
                return

        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Could not read feedback file: {e}", level="WARNING")
            return

        cycles = data.get("cycles", [])
        if not cycles:
            return

        buckets, centroids, skipped_unconfirmed = wfb.aggregate_cycles(cycles, self.PROGRAMME_PROFILES)
        self._history_centroids = centroids

        self.log("=== Washer programme feedback summary ===", level="INFO")
        if skipped_unconfirmed:
            self.log(f"  Skipped {skipped_unconfirmed} unconfirmed cycle(s) for learning", level="INFO")
        for learn_key, bucket in sorted(buckets.items()):
            n = len(bucket["durations"])
            avg = sum(bucket["durations"]) / n
            correct = bucket["correct"]
            total = bucket["total"]
            acc = f"{correct}/{total} ({100*correct//total}%)" if total else "-"
            prog = bucket["prog"]
            temp = bucket["temp"]
            profile = self._get_profile(prog, temp)
            manual = profile.get("duration_min", 180)
            label = profile.get("label", prog)
            temp_str = f" {temp}" if temp else ""
            self._learned_durations[learn_key] = {"n": n, "avg": avg}
            effective = self._get_programme_duration(prog, temp)
            self.log(
                f"  {self._log_safe(label)}{self._log_safe(temp_str):<14} confirmed {n:>2}x  accuracy {acc:<12} "
                f"manual {manual:>3}min  learned {avg:>5.1f}min  effective {effective:>3}min",
                level="INFO",
            )
        self.log("==========================================", level="INFO")

    def _migrate_feedback_add_completion_class(self, dry_run: bool = True):
        """Idempotent migration: add completion_class, valid_for_learning, validation_flags to existing feedback.
        If a record already has completion_class and valid_for_learning and versions are current, skip.
        dry_run: only log summary with counts (completed, interrupted, suspect, learnable, quarantined, unchanged); do not write."""
        import json
        path = self.feedback_file
        if not os.path.exists(path):
            self.log("No feedback file to migrate", level="INFO")
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Could not read feedback for migration: {e}", level="WARNING")
            return
        cycles = data.get("cycles", [])
        if not cycles:
            return
        profile_version = wfb.PROFILE_VERSION
        validation_version = wfb.VALIDATION_VERSION
        counts = wfb.migrate_records(
            cycles, self._classify_cycle_completion, profile_version, validation_version
        )
        if dry_run:
            self.log(
                f"Feedback migration dry-run: completed={counts.get('completed', 0)} interrupted={counts.get('interrupted', 0)} "
                f"suspect={counts.get('suspect', 0)} learnable={counts['learnable']} quarantined={counts['quarantined']} unchanged={counts['unchanged']}",
                level="INFO",
            )
            return
        data["migration_version"] = "1"
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write migrated feedback: {e}", level="WARNING")
            return
        self.log(
            f"Feedback migration applied: completed={counts.get('completed', 0)} interrupted={counts.get('interrupted', 0)} "
            f"suspect={counts.get('suspect', 0)} learnable={counts['learnable']} quarantined={counts['quarantined']} unchanged={counts['unchanged']}",
            level="INFO",
        )

    def _save_cycle_feedback(
        self,
        predicted: str,
        predicted_temperature,
        confirmed: str,
        confirmed_temperature,
        duration_min: float,
        energy_kwh: float,
        heating_bursts: int,
        max_power_w: float,
        spin_rpm: int | None = None,
        user_confirmed: bool = False,  # kept for backward compat; same as programme_user_confirmed
        spin_user_confirmed: bool = False,
        duration_source: str | None = None,
        end_reason: str | None = None,
        idle_min: float | None = None,
        confirmed_by: str | None = None,
        effective_end_at: str | None = None,  # When wash program finished (ISO); use for learning/history
        detected_at: str | None = None,  # When automation transitioned (ISO); optional for audit
        completion_class: str | None = None,  # completed | interrupted | suspect
        valid_for_learning: bool | None = None,
        validation_flags: list | None = None,
        transition_path: str | None = None,  # user_cycle_end | anti_crease_pattern | low_power_detected | door_opened_first
        programme_key_used_for_validation: str | None = None,
        profile_version: str | None = None,
        validation_version: str | None = None,
        selected_options: dict | None = None,  # e.g. {"water_plus": "on", "soak": "off"} from option entities
        cost_kr: float | None = None,  # Settled per-cycle cost (self._session_cost_kr); None for migration/backfill saves
        vibration: dict | None = None,  # Telemetry only (see _vibration_summary); None for migration/backfill saves
        actor_start: dict | None = None,  # Who loaded the machine (self._cycle_actor; see _attribute); None for migration/backfill saves
        actor_empty: dict | None = None,  # Who emptied it - ONLY when observed in this same save (Running direct to Emptied); the Unemptied-first path patches emptied_by later via _patch_cycle_record
    ):
        """Append one completed cycle record to the feedback JSON file (v2 format).

        duration_min is always the programme length (actual machine run). It does NOT include
        idle time after the programme ended (e.g. time until door open or low-power detection).
        When we correct from history, we store idle_min = excluded minutes so it's explicit.

        effective_end_at: when the wash program is considered finished (e.g. start of anti-crease tail).
        detected_at: when the automation realized the cycle had finished. Learning/history use effective_end_at.

        duration_source: "user_cycle_end" | "history_corrected" when we used that for duration_min.
        end_reason: "low_power_detected" | "door_opened_first" | "user_cycle_end" | "anti_crease_pattern".
        idle_min: minutes from programme end to when we recorded (door open / detection); only set when we corrected.

        actor_start/actor_empty: ActorAttribution results (see _attribute) for who loaded / who
        emptied the machine. started_by and attribution.start are written on every save from here
        on - an absent key on an old record just means it predates this feature - and a plain
        JSON null is fine when unknown (the AppDaemon 4.5.13 None-dropping bug only applies to
        HA entity attributes, not this file). emptied_by/emptied_ts/attribution.empty are only
        written when actor_empty is given (the machine went Running straight to Emptied, so both
        are known at save time); the far more common Unemptied-first path saves without them and
        _patch_cycle_record fills them in later when the door actually opens.

        Returns the saved record dict, or None if the save was skipped (duplicate guard) or failed
        (write error). Callers use the returned record to decide whether to trigger a confirm push
        (see _maybe_send_confirm_push) - only for a genuinely new save of the live cycle.
        """
        import json
        import os

        record = wfb.build_cycle_record(
            ts=self._format_local(self._now_utc()),
            predicted=predicted,
            predicted_temperature=predicted_temperature,
            confirmed=confirmed,
            confirmed_temperature=confirmed_temperature,
            duration_min=duration_min,
            energy_kwh=energy_kwh,
            heating_bursts=heating_bursts,
            max_power_w=max_power_w,
            spin_rpm=spin_rpm,
            user_confirmed=user_confirmed,
            spin_user_confirmed=spin_user_confirmed,
            duration_source=duration_source,
            end_reason=end_reason,
            idle_min=idle_min,
            confirmed_by=confirmed_by,
            effective_end_at=effective_end_at,
            detected_at=detected_at,
            completion_class=completion_class,
            valid_for_learning=valid_for_learning,
            validation_flags=validation_flags,
            transition_path=transition_path,
            programme_key_used_for_validation=programme_key_used_for_validation,
            profile_version=profile_version,
            validation_version=validation_version,
            selected_options=selected_options,
            cost_kr=cost_kr,
            vibration=vibration,
            actor_start=actor_start,
            actor_empty=actor_empty,
            emptied_ts=(self._format_local(self._now_utc()) if actor_empty is not None else None),
        )

        if os.path.exists(self.feedback_file):
            try:
                with open(self.feedback_file, "r") as f:
                    data = json.load(f)
            except Exception:
                data = {"version": 2, "cycles": []}
        else:
            data = {"version": 2, "cycles": []}
            self.log(f"Feedback file will be created at: {self.feedback_file}", level="INFO")

        data["version"] = 2

        # Idempotency guard: two cycle-end paths can fire for the same physical cycle seconds
        # apart (e.g. a door open saving via both _transition_to_unemptied and
        # _transition_to_emptied). A genuine next cycle must run min_cycle_minutes first, so a
        # record this close to the previous one with the same programme and (near-)identical
        # duration is the same cycle. Wall-clock durations grow with the detection gap, so the
        # tolerance is the gap plus rounding slack.
        try:
            last = data["cycles"][-1] if data["cycles"] else None
            if last:
                gap_s = (self._now_utc() - datetime.fromisoformat(last["ts"])).total_seconds()
                if wfb.is_duplicate_cycle(last, record, gap_s):
                    self.log(
                        f"Skipping duplicate cycle feedback: {record['confirmed']} {record['duration_min']} min "
                        f"already recorded {gap_s:.0f}s ago (ts {last['ts']}) by another cycle-end path",
                        level="WARNING",
                    )
                    return
        except Exception as e:
            self.log(f"Duplicate-feedback guard error (saving anyway): {e}", level="DEBUG")

        data["cycles"].append(record)

        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write feedback file {self.feedback_file}: {e}", level="WARNING")
            return

        # Update in-memory learned durations and centroids only when valid for learning
        avg_new = None
        if valid_for_learning:
            avg_new = wfb.apply_learned_sample(
                self._learned_durations,
                self._history_centroids,
                wp.learn_key_for(self.PROGRAMME_PROFILES, confirmed, confirmed_temperature),
                duration_min,
                energy_kwh,
                heating_bursts,
                self._get_profile(confirmed, confirmed_temperature).get("heats"),
            )

        match = "OK" if predicted == confirmed else f"corrected (predicted {predicted})"
        source = "user confirmed" if user_confirmed else "calculated"
        eff = self._get_programme_duration(confirmed, confirmed_temperature)
        spin_str = f"  spin {spin_rpm} rpm" if spin_rpm is not None else ""
        temp_str = f" {confirmed_temperature}" if confirmed_temperature else ""
        label = self._get_profile(confirmed, confirmed_temperature).get("label", confirmed)
        duration_note = f"  [duration from {duration_source}]" if duration_source else ""
        idle_note = f"  (idle {idle_min:.0f} min excluded)" if idle_min is not None and idle_min >= 0 else ""
        end_note = f"  end_reason={end_reason}" if end_reason else ""
        learned_note = f"learned avg now {avg_new:.1f}min  " if avg_new is not None else ""
        self.log(
            f"Feedback saved: {self._log_safe(label)}{self._log_safe(temp_str)} "
            f"- {match}  ({source})  duration {duration_min:.0f}min  energy {energy_kwh:.2f}kWh{spin_str}{duration_note}{idle_note}{end_note}  "
            f"{learned_note}effective ETA {eff}min",
            level="INFO",
        )
        if vibration:
            self.log(
                f"Vibration telemetry: {vibration['pulse_count']} pulses / {vibration['on_seconds']:.0f}s on during cycle "
                f"(first {self._strftime_local(self.first_vibration_at)}, last {self._strftime_local(self.last_vibration_at)})",
                level="INFO",
            )
        return record

    # =========================================================================
    # Vibration telemetry (TELEMETRY ONLY - see initialize() and washer.yaml).
    # The callback, the summary/patch helpers below, and the two live
    # _save_cycle_feedback call sites are the ONLY consumers of these values;
    # nothing here may influence a state transition, classification, delayed-
    # start, ETA, or push decision.
    # =========================================================================

    def _vibration_changed(self, entity, attr, old, new, kwargs):
        """Record pulses only - never gates anything. Wrapped defensively like the app's other
        listen_state callbacks: a flapping battery Zigbee sensor must never break the app."""
        try:
            if new in ("unknown", "unavailable") or old in ("unknown", "unavailable"):
                # Battery Zigbee devices report these often; not worth logging above DEBUG.
                self._vibration_on_started = None
                return
            now = self._now_utc()
            if new == "on":
                self._vibration_events.append(now)
                self._vibration_on_started = (now, self.state == "Running")
                if self.state == "Running":
                    self.vibration_pulse_count += 1
                    if self.first_vibration_at is None:
                        self.first_vibration_at = now
                    self.last_vibration_at = now
            elif new == "off":
                if self._vibration_on_started is not None:
                    started_at, was_running = self._vibration_on_started
                    if was_running:
                        # Cap a single pulse at 600s - sanity guard in case an off-edge was
                        # missed (Zigbee drop) and this "pulse" is actually hours long.
                        elapsed = min((now - started_at).total_seconds(), 600.0)
                        self.vibration_on_seconds += elapsed
                    self._vibration_on_started = None
        except Exception as e:
            self.log(f"Vibration telemetry callback error (ignored): {e}", level="DEBUG")

    def _vibration_summary(self) -> dict | None:
        """Snapshot of this cycle's vibration telemetry for the feedback record. None when
        there is nothing to say: sensor not configured, or configured but silent all cycle -
        most of a normal wash (fill, wash, rinse, balanced spin) is vibration-quiet, so silence
        alone is never evidence the machine wasn't running."""
        if not self.vibration_sensor:
            return None
        if self.vibration_pulse_count == 0 and self.vibration_on_seconds == 0:
            return None
        return {
            "pulse_count": int(self.vibration_pulse_count),
            "on_seconds": round(float(self.vibration_on_seconds), 1),
            "first_at": self._format_local(self.first_vibration_at) if self.first_vibration_at else None,
            "last_at": self._format_local(self.last_vibration_at) if self.last_vibration_at else None,
        }

    def _schedule_vibration_unload_patch(self, saved_record):
        """10 min after a cycle's feedback is saved, count vibration pulses in that window
        (candidate 'someone unloaded / bumped the machine' signal) and patch it onto the saved
        record - see _patch_unload_vibration. Only one pending timer is kept; a second save
        within the window cancels and replaces it rather than tracking both. Acceptable here
        because this is telemetry only, not something learning/ETA logic depends on."""
        if not self.vibration_sensor:
            return
        ts = (saved_record or {}).get("ts")
        if not ts:
            return
        self._safe_cancel_timer(self._unload_patch_timer)
        self._unload_patch_timer = self.run_in(
            self._patch_unload_vibration, 600, ts=ts, save_moment=self._format_utc(self._now_utc()),
        )

    def _patch_unload_vibration(self, kwargs):
        """Patch unload_pulse_count/unload_window_min onto the cycle record named by ts (see
        _schedule_vibration_unload_patch). Telemetry only - gives up quietly at WARNING if the
        file or record is gone; never worth breaking anything else over."""
        self._unload_patch_timer = None
        ts = kwargs.get("ts")
        save_moment = _parse_utc(kwargs.get("save_moment"))
        if not ts or not save_moment:
            return
        window_end = save_moment + timedelta(minutes=10)
        pulse_count = sum(1 for t in self._vibration_events if save_moment <= t <= window_end)

        import json
        if not self.feedback_file or not os.path.exists(self.feedback_file):
            self.log(f"Vibration unload patch: feedback file missing for ts={ts}", level="WARNING")
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Vibration unload patch: could not read feedback file: {e}", level="WARNING")
            return
        cycles = data.get("cycles", [])
        rec = next((c for c in cycles if c.get("ts") == ts), None)
        if rec is None:
            self.log(f"Vibration unload patch: cycle ts={ts} not found (skipping)", level="WARNING")
            return
        rec.setdefault("vibration", {})["unload_pulse_count"] = pulse_count
        rec["vibration"]["unload_window_min"] = 10
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Vibration unload patch: could not write feedback file: {e}", level="WARNING")
            return
        self.log(f"Vibration unload patch: {pulse_count} pulse(s) in the 10 min after cycle ts={ts}", level="DEBUG")

    def _patch_cycle_record(self, ts, updates: dict):
        """Patch `updates` onto the feedback record named by ts (see _schedule_vibration_unload_patch
        / _patch_unload_vibration, which this mirrors). A dict value is merged into any existing
        dict at that key instead of replacing it wholesale - e.g. an "attribution" patch adds
        "empty" alongside the "start" key written at save time, rather than erasing it.

        Used for the Unemptied-first emptying path (_transition_to_emptied "Path B"): the record
        was already written by _transition_to_unemptied before anyone touched the door, so who
        emptied it can only be added after the fact. Best-effort enrichment of a record that may
        not exist - never core cycle logic.

        Two robustness rules, both learned from this codebase's recurring restart theme:

        * No ts -> patch the NEWEST record. Unemptied routinely outlives the process (it lasts
          until a human walks over, and this box gets redeployed constantly), so
          self._last_saved_record_ts is None for most real emptyings. The newest record is
          provably the right target: a later cycle cannot have completed while the machine sat
          Unemptied, because emptying it is the very event being recorded. Same pattern the
          dishwasher has always used (_mark_last_cycle_emptied patches cycles[-1]).
        * Never re-stamp an already-emptied record. self._last_saved_record_ts is only assigned
          when a save actually returns a record, so a failed save leaves the PREVIOUS cycle's ts
          in place - patching that would credit this emptying to an older cycle. An existing
          emptied_ts is the tell, and the same guard makes a repeated door-open idempotent."""
        import json
        if not self.feedback_file or not os.path.exists(self.feedback_file):
            self.log(f"Cycle record patch: feedback file missing for ts={ts}", level="DEBUG")
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Cycle record patch: could not read feedback file: {e}", level="DEBUG")
            return
        cycles = data.get("cycles", [])
        if ts:
            rec = next((c for c in cycles if c.get("ts") == ts), None)
            if rec is None:
                # A ts we were given but cannot find is genuinely odd (it came from a save that
                # reported success), so this one is worth seeing in the log.
                self.log(f"Cycle record patch: cycle ts={ts} not found (skipping)", level="WARNING")
                return
        else:
            rec = cycles[-1] if cycles else None
            if rec is None:
                self.log("Cycle record patch: no ts and no records to patch (skipping)", level="DEBUG")
                return
            self.log(
                f"Cycle record patch: no ts in memory (restart while Unemptied) - falling back "
                f"to the newest record ts={rec.get('ts')}",
                level="INFO",
            )
        if "emptied_ts" in updates and rec.get("emptied_ts"):
            self.log(
                f"Cycle record patch: record ts={rec.get('ts')} is already marked emptied "
                f"({rec.get('emptied_ts')}) - not re-stamping",
                level="DEBUG",
            )
            return
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(rec.get(key), dict):
                rec[key].update(value)
            else:
                rec[key] = value
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Cycle record patch: could not write feedback file: {e}", level="WARNING")
            return
        self.log(
            f"Cycle record patch: updated {list(updates.keys())} on ts={rec.get('ts')}",
            level="DEBUG",
        )

    # =========================================================================
    # Presence-gated confirm push (feature: ask whoever is home to confirm the
    # programme with one tap when a cycle ends unconfirmed but worth learning)
    # =========================================================================

    # Confirm-push gate and the WASHER_CONFIRM|<ts>|<prog>|<temp> action codec.
    _should_send_confirm_push = staticmethod(wfb.should_send_confirm_push)
    _encode_confirm_action = staticmethod(wfb.encode_confirm_action)
    _parse_confirm_action = staticmethod(wfb.parse_confirm_action)

    def _maybe_send_confirm_push(self, record: dict):
        """Called right after a successful _save_cycle_feedback of the live cycle
        (Unemptied / door-opened-first transitions). Sends the confirm push only
        when _should_send_confirm_push gates it through."""
        if not self._should_send_confirm_push(record, self.confirm_push_enabled):
            return
        self._send_confirm_push(record)

    # =========================================================================
    # Actor attribution ("who started this wash / who emptied it") via the shared
    # ActorAttribution app. Its API is frozen: get_app("ActorAttribution") is resolved
    # LAZILY on every call, never cached at initialize - see 4086e2e: dependencies: is
    # only for get_app results cached at initialize; declaring one here would reload this
    # monitor (discarding live cycle state) every time ActorAttribution changes.
    # =========================================================================

    def _unknown_actor(self, reason: str, at=None) -> dict:
        """Build an unknown-attribution dict in the exact shape ActorAttribution.attribute()
        documents (all keys present), so a resolution failure never causes a downstream
        KeyError - see _attribute()."""
        now = self._now_utc()
        anchor = at if at is not None else now
        return {
            "person": None,
            "method": "unknown",
            "reason": reason,
            "people_home": [],
            "anchor": self._format_utc(anchor) if hasattr(anchor, "astimezone") else (anchor or ""),
            "evaluated_at": self._format_utc(now),
            "version": 1,
        }

    def _cycle_actor_from_state_attrs(self, attrs: dict) -> dict:
        """Rebuild a _cycle_actor-shaped dict from started_by/started_by_method persisted on
        the state entity. Used when a cycle resumes without re-invoking _begin_running_cycle -
        an AppDaemon reload mid-Running restore (_restore_running_state), or
        _recover_from_false_unemptied reverting a false finish - so self._cycle_actor would
        otherwise fall back to unknown even though we captured it before. "" is AppDaemon
        4.5.13 dropping a None/False/0-valued attribute (see smart_cooling.py's _publish()),
        not a real value - treated the same as missing, like every other restore field here."""
        person = attrs.get("started_by") or None
        method = attrs.get("started_by_method") or None
        result = self._unknown_actor("restored_from_entity")
        result["person"] = person
        result["method"] = method or "unknown"
        return result

    def _attribute(self, event, at=None) -> dict:
        """Resolve who performed a physical action via the ActorAttribution app.

        Lazy get_app per call (no dependencies: entry - see the section banner above). Never
        lets an attribution problem break a cycle: any failure returns the unknown dict, in the
        same shape as a real result, so callers can always do result.get("person") /
        result.get("method") without a KeyError.
        """
        try:
            app = self.get_app("ActorAttribution")
        except Exception as e:
            self.log(f"Could not resolve ActorAttribution app for event={event!r}: {e}", level="WARNING")
            return self._unknown_actor("attribution_error", at)
        if app is None:
            self.log(f"ActorAttribution app not found - cannot attribute event={event!r}", level="WARNING")
            return self._unknown_actor("attribution_app_missing", at)
        try:
            result = app.attribute(event, at=at)
            if not isinstance(result, dict):
                self.log(
                    f"ActorAttribution.attribute returned {type(result).__name__}, expected dict, for event={event!r}",
                    level="WARNING",
                )
                return self._unknown_actor("attribution_error", at)
            return result
        except Exception as e:
            self.log(f"ActorAttribution.attribute raised for event={event!r}: {e}", level="WARNING")
            return self._unknown_actor("attribution_error", at)

    def _send_confirm_push(self, record: dict):
        """Build and send the confirm-programme push for `record` (unconditional -
        callers gate as needed; the washer_test_confirm_push test hook calls this
        directly to preview the message/buttons). Same pattern as _push_mobile.

        Targets the person who started the wash (started_by on the record) directly when
        confirm_push_target_actor is set and one is known - MobileNotifier's list-target
        semantics bypass category_audience and reach them even after they've left home
        (apps/notify/mobile_notifier.py); otherwise falls back to the usual broadcast target."""
        try:
            predicted = record.get("predicted") or ""
            ts = record.get("ts", "")
            title = "Washer finished"
            if predicted and predicted != "unknown":
                predicted_temp_storage = record.get("predicted_temperature")
                predicted_temp = self._temp_from_storage(predicted_temp_storage)
                profile = self._get_profile(predicted, predicted_temp)
                label = profile.get("label", predicted)
                temp_suffix = f" {predicted_temp}" if predicted_temp else ""
                message = f"Was it {label}{temp_suffix}? Confirming teaches the ETA."
                action_id = self._encode_confirm_action(ts, predicted, predicted_temp_storage)
                actions = [
                    {"action": action_id, "title": f"Yes, {label}{temp_suffix}"},
                    {"action": "URI", "title": "Other...", "uri": self.confirm_push_dashboard_uri},
                ]
            else:
                message = "Which program did you run? Confirming teaches the ETA."
                actions = [
                    {"action": "URI", "title": "Choose in dashboard", "uri": self.confirm_push_dashboard_uri},
                ]
            notifier = self.get_app("MobileNotifier")
            if notifier is None:
                self.log("MobileNotifier app not found - cannot send confirm push", level="WARNING")
                return
            target = self._confirm_push_target_for(record)
            self.create_task(notifier.notify(
                title=title,
                message=message,
                target=target,
                data={"data": {"actions": actions, "tag": "washer_confirm"}},
                category="washer_confirm",
            ))
            self.log(f"Confirm push sent for cycle ts={ts} predicted={predicted} target={target}", level="INFO")
        except Exception as e:
            self.log(f"Could not send confirm push: {e}", level="WARNING")

    def _confirm_push_target_for(self, record: dict):
        """Same target resolution _send_confirm_push uses, so a dismiss (clear_notification)
        reaches the same device(s) the original confirm push was sent to."""
        started_by = record.get("started_by")
        return [started_by] if (self.confirm_push_target_actor and started_by) else self.confirm_push_target

    def _dismiss_confirm_push(self, record: dict):
        """Clear an already-delivered confirm push once the cycle gets confirmed by any
        route (push button, dashboard picker, or Off-state confirm) - otherwise it just
        sits on-device looking unresolved even though the backend now considers it
        confirmed. Safe to call even if no push was ever sent (no-op on the device)."""
        try:
            notifier = self.get_app("MobileNotifier")
            if notifier is None:
                return
            target = self._confirm_push_target_for(record)
            self.create_task(notifier.clear_notification(tag="washer_confirm", target=target, category="washer_confirm"))
        except Exception as e:
            self.log(f"Could not dismiss confirm push: {e}", level="DEBUG")

    def _on_confirm_push_action(self, event_name, data, kwargs):
        """Handle WASHER_CONFIRM|<ts>|<prog>|<temp> button presses from the confirm push
        (see _send_confirm_push). Sync handler - this app has no async def anywhere and
        listen_event callbacks elsewhere in this codebase (dishwasher_monitor's force-*
        events, wakeup_reminder's notification-action handler) are plain sync methods too.
        """
        data = data or {}
        action = data.get("action", "") if isinstance(data, dict) else ""
        if not action.startswith("WASHER_CONFIRM|"):
            return
        parsed = self._parse_confirm_action(action)
        if not parsed:
            self.log(f"Malformed confirm-push action ignored: {action!r}", level="WARNING")
            return
        ts, prog, temp = parsed

        import json
        if not self.feedback_file or not os.path.exists(self.feedback_file):
            self.log(f"confirm action for unknown/stale cycle ts={ts}", level="INFO")
            return
        try:
            with open(self.feedback_file, "r") as f:
                fb_data = json.load(f)
        except Exception as e:
            self.log(f"Could not read feedback file for confirm action: {e}", level="WARNING")
            return
        cycles = fb_data.get("cycles", [])
        rec = next((c for c in cycles if c.get("ts") == ts), None)
        if rec is None:
            self.log(f"confirm action for unknown/stale cycle ts={ts}", level="INFO")
            return
        if rec.get("programme_confirmed_by_human") or rec.get("programme_user_confirmed"):
            self.log(f"Cycle ts={ts} already confirmed, ignoring", level="INFO")
            return

        rec["confirmed"] = prog
        rec["confirmed_temperature"] = self._temp_for_storage(temp)
        rec["programme_confirmed_by_human"] = True
        rec["programme_user_confirmed"] = True  # keep legacy field consistent
        rec["programme_confirmed_via"] = "push_action"

        try:
            with open(self.feedback_file, "w") as f:
                json.dump(fb_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write feedback file for confirm action: {e}", level="WARNING")
            return
        self._dismiss_confirm_push(rec)

        # Update in-memory learned durations incrementally - same math and learn-key
        # derivation as _save_cycle_feedback's save-time update, gated the same way the
        # loader gates (valid_for_learning and a positive duration).
        conf_temp_internal = self._temp_from_storage(rec.get("confirmed_temperature"))
        learn_key = f"{prog}|{conf_temp_internal}" if (conf_temp_internal and self._programme_has_temperature(prog)) else prog
        duration_min = rec.get("duration_min")
        if rec.get("valid_for_learning") and isinstance(duration_min, (int, float)) and duration_min > 0:
            prev = self._learned_durations.get(learn_key, {"n": 0, "avg": duration_min})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + duration_min) / n_new
            self._learned_durations[learn_key] = {"n": n_new, "avg": avg_new}

        self.log(f"Cycle {ts} confirmed as {learn_key} via push action", level="INFO")

    def _on_test_confirm_push(self, event_name, data, kwargs):
        """Test hook: washer_test_confirm_push - send the confirm push for the LAST
        feedback record regardless of its confirmed state, so the real message/buttons
        can be checked on the phone without waiting for a cycle to end unconfirmed."""
        import json
        if not self.feedback_file or not os.path.exists(self.feedback_file):
            self.log("Test confirm push: no feedback file found", level="INFO")
            return
        try:
            with open(self.feedback_file, "r") as f:
                fb_data = json.load(f)
        except Exception as e:
            self.log(f"Test confirm push: could not read feedback file: {e}", level="WARNING")
            return
        cycles = fb_data.get("cycles", [])
        if not cycles:
            self.log("Test confirm push: feedback file has no cycles", level="INFO")
            return
        record = cycles[-1]
        self.log(
            f"Test confirm push: sending for last cycle ts={record.get('ts')} "
            f"predicted={record.get('predicted')} confirmed={record.get('confirmed')}",
            level="INFO",
        )
        self._send_confirm_push(record)

    def _update_last_feedback_user_confirmed(self, prog_key: str, temp: str | None, confirmed_by: str | None, only_if_recent: bool = False):
        """When user confirms programme (e.g. while in Unemptied/Emptied or after), mark the last feedback record as user-confirmed and re-classify.
        If only_if_recent is True (e.g. we're in Off), only update when the last record's ts is within the last 12 hours."""
        import json
        import os
        from datetime import datetime, timezone, timedelta
        if not self.feedback_file or not os.path.exists(self.feedback_file):
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Could not read feedback for user-confirm update: {e}", level="WARNING")
            return
        cycles = data.get("cycles", [])
        if not cycles:
            return
        rec = cycles[-1]
        if only_if_recent and rec.get("ts"):
            try:
                # Parse ts (ISO with or without Z / +01:00)
                ts_str = rec["ts"]
                if "T" in ts_str:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    return
                if (self._now_utc() - dt).total_seconds() > 12 * 3600:
                    return  # Last record older than 12 h, don't update
            except Exception:
                pass
        rec["programme_user_confirmed"] = True
        if confirmed_by:
            rec["confirmed_by"] = confirmed_by
        # Optionally correct confirmed programme if user set something different
        temp_stored = self._temp_for_storage(temp)
        if rec.get("confirmed") != prog_key or rec.get("confirmed_temperature") != temp_stored:
            rec["confirmed"] = prog_key
            rec["confirmed_temperature"] = temp_stored
        # Re-classify with user_confirmed so valid_for_learning can change (e.g. suspect -> completed)
        conf_temp = self._temp_from_storage(rec.get("confirmed_temperature"))
        pred = rec.get("predicted", "")
        pred_temp = self._temp_from_storage(rec.get("predicted_temperature"))
        transition_path = rec.get("transition_path") or rec.get("end_reason") or "low_power_detected"
        if transition_path not in ("user_cycle_end", "anti_crease_pattern", "low_power_detected", "door_opened_first", "tail_to_standby"):
            transition_path = "low_power_detected"
        classification = self._classify_cycle_completion(
            run_minutes=float(rec.get("duration_min", 0)),
            energy_kwh=float(rec.get("energy_kwh", 0) or 0),
            heating_bursts=int(rec.get("heating_bursts", 0) or 0),
            max_power_w=float(rec.get("max_power_w", 0) or 0),
            predicted=pred,
            predicted_temperature=pred_temp,
            confirmed=prog_key,
            confirmed_temperature=conf_temp,
            transition_path=transition_path,
            spin_rpm=rec.get("spin_rpm"),
            user_confirmed_override=True,
        )
        rec["completion_class"] = classification["completion_class"]
        rec["valid_for_learning"] = classification["valid_for_learning"]
        rec["validation_flags"] = classification["validation_flags"]
        rec["programme_key_used_for_validation"] = classification.get("programme_key_used_for_validation", "")
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write feedback after user-confirm update: {e}", level="WARNING")
            return
        self._dismiss_confirm_push(rec)
        label = self._get_profile(prog_key, conf_temp).get("label", prog_key)
        self.log(
            f"Updated last feedback: programme_user_confirmed=True, valid_for_learning={classification['valid_for_learning']} ({label})",
            level="INFO",
        )

    def _parse_spin_rpm(self, value: str) -> int | None:
        """Parse spin speed from input_select state. Returns rpm (0 = no spin) or None."""
        return wcls.parse_spin_rpm(value)

    def _build_user_id_cache(self):
        """Build a {user_id: display_name} map from HA person.* entities.

        Each person entity carries a user_id attribute that matches the context.user_id
        recorded when a human changes a helper in the UI. Falls back gracefully if the
        person domain is empty or unavailable.
        """
        try:
            all_states = self.get_state("person") or {}
            for entity_id, state_data in all_states.items():
                attrs = (state_data or {}).get("attributes", {}) if isinstance(state_data, dict) else {}
                user_id = attrs.get("user_id")
                name = attrs.get("friendly_name") or entity_id.split(".", 1)[-1].replace("_", " ").title()
                if user_id:
                    self._user_id_to_name[user_id] = name
            if self._user_id_to_name:
                self.log(f"User ID cache built: {list(self._user_id_to_name.values())}", level="DEBUG")
        except Exception as e:
            self.log(f"Could not build user ID cache: {e}", level="DEBUG")

    def _on_confirm_changed(self, entity, attribute, old, new, kwargs):
        """Called when input_select.washer_confirmed_programme or temperature changes.

        Programme confirmation is critical: we set programme_confirmed_by_user = True
        whenever the selector is set to a real programme (not Auto), so ETA and guards
        use the selected programme immediately. We only skip when the app itself wrote
        the selector (auto-detection), using _skip_next_confirm. user_id from context
        is used only for programme_confirmed_by (who); if missing we still confirm.
        """
        # Resetting to Auto is authoritative regardless of source (user or internal reset).
        if entity == self.confirm_entity and new in (None, "unknown", "unavailable", "Auto (unconfirmed)"):
            was = old if old not in (None, "unknown", "unavailable") else None
            self.log(
                f"Programme set to Auto (unconfirmed) - classification can override; was: {was!r}"
                if was
                else "Programme set to Auto (unconfirmed) - classification can override",
                level="INFO",
            )
            self.programme_confirmed_by_user = False
            self.confirmed_by_username = None
            # Persist immediately so a restart before the next energy tick doesn't
            # restore stale programme_confirmed_by_user=True from the state entity.
            try:
                full = self.get_state(self.state_entity, attribute="all") or {}
                attrs = dict((full.get("attributes") or {}))
                attrs["programme_confirmed_by_user"] = False
                attrs["programme_confirmed_by"] = ""
                current_state = self.get_state(self.state_entity) or "Running"
                # programme_confirmed_by_user is always False here (user reset the selector to
                # Auto) -- AppDaemon 4.5.13 set_state bug, not ours; see smart_cooling.py's
                # _publish() for details.
                self._set_state_entity( state=current_state, attributes=attrs, replace=True)
            except Exception:
                pass
            return

        # App just set the selector (auto-detection); do not treat as user confirmation.
        if getattr(self, "_skip_next_confirm", False):
            self._skip_next_confirm = False
            return

        # Temperature-only change: only mark as confirmed if the programme dropdown
        # is already set to a real programme (not "Auto"). Changing temperature while
        # programme is still auto-detected is just a hint, not a confirmation.
        if entity == self.temperature_entity:
            try:
                prog_label = self.get_state(self.confirm_entity) if self.confirm_entity else None
                if not prog_label or prog_label in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    self.log(f"Temperature changed to '{new}' but programme is still Auto - not marking as confirmed", level="DEBUG")
                    return
            except Exception:
                return

        # Selector changed to a real programme (or temp with programme set) - treat as confirmation.
        # user_id is optional: we use it for "who" (programme_confirmed_by) when HA passes context; confirmation is recorded either way.
        ctx = (kwargs.get("context") or {}) if kwargs else {}
        user_id = ctx.get("user_id")
        self.programme_confirmed_by_user = True
        self.confirmed_by_username = self._user_id_to_name.get(user_id, user_id) if user_id else None

        # Persist both flags as state entity attributes so they survive an app reload.
        # Without this, the restore logic can't tell user-confirmed from auto-detected.
        current_state = self.get_state(self.state_entity) or "Running"
        try:
            full = self.get_state(self.state_entity, attribute="all")
            attrs = dict((full or {}).get("attributes") or {})
            attrs["programme_confirmed_by_user"] = True
            attrs["programme_confirmed_by"] = self.confirmed_by_username or ""
            self._set_state_entity( state=current_state, attributes=attrs, replace=True)
        except Exception:
            pass

        # Update ETA immediately when Running so the UI shows the correct remaining time without waiting for the next energy tick.
        if current_state == "Running" and self.start_time:
            self._push_running_eta_attributes()

        # If we're in Unemptied, Emptied, or Off (and last cycle was recent), user may be confirming the last cycle - update that feedback record.
        if current_state in ("Unemptied", "Emptied", "Off"):
            try:
                prog_key = None
                temp = None
                if entity == self.confirm_entity:
                    prog_key = self._LABEL_TO_KEY.get(new, "unknown")
                    temp = self._read_temperature_selector()
                else:
                    prog_label = self.get_state(self.confirm_entity) if self.confirm_entity else None
                    prog_key = self._LABEL_TO_KEY.get(prog_label, "unknown") if prog_label else "unknown"
                    temp = new
                if prog_key and prog_key != "unknown":
                    self._update_last_feedback_user_confirmed(prog_key, temp, self.confirmed_by_username, only_if_recent=(current_state == "Off"))
            except Exception as e:
                self.log(f"Could not update last feedback for user confirm: {e}", level="DEBUG")

        if entity == self.confirm_entity:
            prog_key = self._LABEL_TO_KEY.get(new, "unknown")
            if prog_key and prog_key != "unknown":
                self._apply_programme_ui_dropdowns(prog_key)
            temp = self._read_temperature_selector()
            if prog_key and prog_key != "unknown":
                temp_str = f" + {temp}" if temp else ""
                by_str = f" by {self.confirmed_by_username}" if self.confirmed_by_username else ""
                self.log(f"User confirmed/corrected programme: '{new}'{self._log_safe(temp_str)} (key: {prog_key}){by_str}", level="INFO")
                # Upgrade expected_dur_at_start when user selects a longer programme (avoids
                # false finish from earlier misclassification e.g. Ekspres vs Bomuld 60).
                user_dur = self._get_programme_duration(prog_key, temp, use_learned=False)
                if user_dur and (self.expected_dur_at_start is None or user_dur > self.expected_dur_at_start):
                    old = self.expected_dur_at_start
                    self.expected_dur_at_start = user_dur
                    self._guard_bar_class = (prog_key, temp)
                    self.log(f"Upgraded expected_dur_at_start: {old} -> {user_dur:.0f} min (user confirmed {new})", level="INFO")
        elif entity == self.temperature_entity:
            by_str = f" by {self.confirmed_by_username}" if self.confirmed_by_username else ""
            self.log(f"User set temperature to '{self._log_safe(new)}'{by_str} (programme already confirmed)", level="INFO")
            # Upgrade expected_dur when user sets temperature (e.g. Bomuld 60 vs 40).
            prog_label = self.get_state(self.confirm_entity) if self.confirm_entity else None
            prog_key = self._LABEL_TO_KEY.get(prog_label, "unknown") if prog_label else "unknown"
            temp = self._read_temperature_selector()
            if prog_key and prog_key != "unknown":
                user_dur = self._get_programme_duration(prog_key, temp, use_learned=False)
                if user_dur and (self.expected_dur_at_start is None or user_dur > self.expected_dur_at_start):
                    old = self.expected_dur_at_start
                    self.expected_dur_at_start = user_dur
                    self._guard_bar_class = (prog_key, temp)
                    self.log(f"Upgraded expected_dur_at_start: {old} -> {user_dur:.0f} min (user set temp {new})", level="INFO")

    def _push_running_eta_attributes(self):
        """Update state entity with current ETA from the selected programme. Call when user confirms programme during Running so the UI updates immediately."""
        if not self.start_time:
            return
        try:
            label = self.get_state(self.confirm_entity) if self.confirm_entity else None
            if not label or label in ("Auto (unconfirmed)", "unknown", "unavailable"):
                return
            eta_prog = self._LABEL_TO_KEY.get(label, "unknown")
            if eta_prog == "unknown":
                return
            eta_temp = self._read_temperature_selector() if self.temperature_entity else None
            effective_dur = self._get_programme_duration(eta_prog, eta_temp, use_learned=False)
            if not effective_dur:
                return
            elapsed_min = (self._now_utc() - self.start_time).total_seconds() / 60
            remaining = max(0, round(effective_dur - elapsed_min))
            est_end = self.start_time + timedelta(minutes=effective_dur)
            full = self.get_state(self.state_entity, attribute="all") or {}
            attrs = dict((full.get("attributes") or {}))
            attrs["programme_duration_min"] = effective_dur
            attrs["estimated_remaining_min"] = remaining
            attrs["estimated_end_time"] = est_end.astimezone(self._local_tz()).strftime("%H:%M")
            attrs["elapsed_minutes"] = round(elapsed_min, 1)
            attrs["progress_pct"] = min(100, max(0, round(100 * elapsed_min / effective_dur))) if effective_dur else 0
            attrs["programme_confirmed_by_user"] = bool(self.programme_confirmed_by_user)
            attrs["programme_confirmed_by"] = self.confirmed_by_username or ""
            if self.expected_dur_at_start is not None:
                attrs["expected_dur_at_start"] = self.expected_dur_at_start
                attrs["expected_dur_key"] = self._guard_bar_key_str()
            attrs["predicted_programme"] = ""
            attrs["predicted_programme_label"] = ""
            attrs["predicted_temperature"] = ""
            # progress_pct/estimated_remaining_min can legitimately be 0 here (just after cycle
            # start, or near the end of the countdown) -- AppDaemon 4.5.13 set_state bug, not
            # ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity( state="Running", attributes=attrs, replace=True)
        except Exception as e:
            self.log(f"Could not push running ETA attributes: {e}", level="DEBUG")

    def _apply_programme_ui_dropdowns(self, prog_key: str):
        """Update temperature and spin dropdowns to match the selected programme.

        Uses profile allowed_temperatures / allowed_spin_speeds and default_*.
        Invalid-value reset: if current value is not in the new allowed list, set to
        programme default or to \"—\" if no default. ECO shows 40-60°C, 40°C, 60°C.
        """
        profile = self.PROGRAMME_PROFILES.get(prog_key, {})
        if not profile:
            return
        no_choice = "—"
        try:
            if self.temperature_entity:
                allowed_temps = profile.get("allowed_temperatures") or []
                # ECO always gets 40-60, 40, 60 so HA dropdown shows all three
                if prog_key == "eco" and set(allowed_temps) != {"40-60°C", "40°C", "60°C"}:
                    allowed_temps = ["40-60°C", "40°C", "60°C"]
                if allowed_temps:
                    temp_options = [no_choice] + list(allowed_temps)
                    self.call_service("input_select/set_options", entity_id=self.temperature_entity, options=temp_options)
                    current = (self.get_state(self.temperature_entity) or "").strip()
                    if current not in temp_options:
                        new_val = profile.get("default_temperature") or no_choice
                        if new_val not in temp_options:
                            new_val = no_choice
                        self.call_service("input_select/select_option", entity_id=self.temperature_entity, option=new_val)
                else:
                    self.call_service("input_select/set_options", entity_id=self.temperature_entity, options=[no_choice])
                    self.call_service("input_select/select_option", entity_id=self.temperature_entity, option=no_choice)
            if self.spin_entity:
                allowed_spin = profile.get("allowed_spin_speeds") or []
                if allowed_spin:
                    spin_options = [no_choice] + list(allowed_spin)
                    self.call_service("input_select/set_options", entity_id=self.spin_entity, options=spin_options)
                    current = (self.get_state(self.spin_entity) or "").strip()
                    if current not in spin_options:
                        new_val = profile.get("default_spin") or no_choice
                        if new_val not in spin_options:
                            new_val = no_choice
                        self.call_service("input_select/select_option", entity_id=self.spin_entity, option=new_val)
                else:
                    self.call_service("input_select/set_options", entity_id=self.spin_entity, options=[no_choice])
                    self.call_service("input_select/select_option", entity_id=self.spin_entity, option=no_choice)
        except Exception as e:
            self.log(f"Could not apply programme UI dropdowns for {prog_key}: {e}", level="DEBUG")

    def _start_energy_detection(self):
        """Start monitoring energy consumption to detect when cycle finishes."""
        if not self.use_energy_detection:
            return
        
        # Get initial energy value
        try:
            energy = self.get_state(self.energy_sensor)
            if energy is not None and energy not in ["unknown", "unavailable"]:
                self.last_energy_value = float(energy)
                self.last_energy_time = self._now_utc()
                self.energy_stable_start_time = None
                self.last_high_energy_at = self.last_energy_time  # Seed: cycle just started = high
                self.energy_buffer = [(self.last_energy_time, self.last_energy_value)]
                self._zero_power_since = None

                # Start checking energy periodically
                self.energy_check_timer = self.run_in(self._check_energy_finish, self.energy_check_interval)
                self.log("Energy-based finish detection started", level="DEBUG")
        except (ValueError, TypeError):
            self.log("Could not get initial energy value for detection", level="WARNING")

    def _maybe_handle_delayed_start(self):
        """Detect a Miele delayed-start wait and slide the cycle start past it.

        The delay-timer selection burst (up to ~60W for ~15 min while the user picks a
        programme) trips start detection; the machine then sits at flat standby for hours
        until the wash actually begins. Left alone, the whole wait gets counted as wash time
        (see washer.yaml DELAYED START block). While gated (feature on, not already trimmed,
        no heating/energy evidence of a real wash yet) this tracks how long power has stayed
        below start_w; once that plateau has lasted at least delay_plateau_minutes and power
        rises back to start_w or above, the moment activity resumes becomes the new start_time.

        As soon as heating or energy above delay_energy_floor_kwh proves the wash is real, we
        stop looking for the rest of the cycle - either we already trimmed, or this was never a
        delayed start (a normal cycle should never reach a qualifying plateau anyway).
        """
        gated = (
            self.detect_delayed_start
            and not self._delayed_start_trimmed
            and not self.observed_heating
            and self._get_energy_used() < self.delay_energy_floor_kwh
        )
        if not gated:
            # Real wash evidence (heating/energy floor) arriving while a qualifying plateau is
            # open IS the resume signal: heating can begin within one heartbeat of the wait
            # ending, so a "power >= start_w but not yet heating" tick may never happen. Slide
            # now rather than leaving the wait uncorrected (and the waiting UI stuck) until the
            # end-of-cycle history backstop.
            if self.detect_delayed_start and not self._delayed_start_trimmed and self._delay_waiting:
                self._slide_start_for_delayed_start(self._now_utc())
            self._delay_plateau_start = None
            self._delay_waiting = False
            return

        now = self._now_utc()
        current_power = self._get_current_power()
        if current_power < self.start_w:
            if self._delay_plateau_start is None:
                self._delay_plateau_start = now
            plateau_min = (now - self._delay_plateau_start).total_seconds() / 60
            if plateau_min >= self.delay_plateau_minutes and not self._delay_waiting:
                self._delay_waiting = True
                self.log(
                    f"Delayed start suspected: flat standby >= {self.delay_plateau_minutes} min "
                    f"(power {current_power:.1f}W < {self.start_w:.0f}W) - ETA paused",
                    level="INFO",
                )
        else:
            if self._delay_plateau_start is not None:
                plateau_min = (now - self._delay_plateau_start).total_seconds() / 60
                if plateau_min >= self.delay_plateau_minutes:
                    self._slide_start_for_delayed_start(now)
            self._delay_plateau_start = None

    def _slide_start_for_delayed_start(self, resume_at):
        """Slide self.start_time to resume_at (activity resumed after a delayed-start wait).

        Re-bases energy accounting and the running watchdog from resume_at: the finish loop's
        stable/idle windows and the max-runtime tripwire must measure from the real wash, not
        from the bogus (pre-wait) start.
        """
        old_start = self.start_time
        lead_min = (resume_at - old_start).total_seconds() / 60
        self._delayed_start_lead_idle_min = round(lead_min, 1)
        self.start_time = resume_at
        # self._cycle_actor is NOT recomputed here - attribution anchors to when the machine was
        # LOADED (door close, see _begin_running_cycle), not when the motor actually starts;
        # sliding start_time for a delayed start must not change who gets credit for the load.
        self._delayed_start_trimmed = True
        self._delay_plateau_start = None
        self._delay_waiting = False

        # Re-base energy accounting to the real wash start.
        try:
            energy = self.get_state(self.energy_sensor)
            if energy is not None and energy not in ["unknown", "unavailable"]:
                self.energy_start = float(energy)
                self.last_energy_value = self.energy_start
                self.last_energy_time = resume_at
                self.energy_buffer = [(resume_at, self.energy_start)]
            else:
                self.energy_buffer = []
        except (ValueError, TypeError):
            self.energy_buffer = []
        # standby wait excluded from cost, same as duration
        self._session_cost_kr = 0.0
        self._cost_prev_energy_kwh = self.energy_start
        self.last_high_energy_at = resume_at
        self.energy_stable_start_time = None
        self.finish_confirmed = False
        self._zero_power_since = None

        # Re-freeze expected duration from the real wash (was frozen from the bogus start tick).
        self.expected_dur_at_start = None
        self._guard_bar_class = None
        self._live_class_key = None
        self._live_class_since = None

        # Re-arm the max-running-hours watchdog from the real start.
        self._safe_cancel_timer(self.running_watchdog_timer)
        self.running_watchdog_timer = self.run_in(
            self._running_watchdog_timeout,
            int(self.max_running_hours * 3600),
        )

        self._push_corrected_start_time_to_entity()
        self.log(
            f"Delayed start: sliding cycle start {self._strftime_local(old_start)} -> "
            f"{self._strftime_local(resume_at)} (trimmed {self._delayed_start_lead_idle_min:.0f} min standby wait)",
            level="INFO",
        )

    def _standby_backstop_tick(self, now, tick_prog, tick_temp, tick_class) -> bool:
        """Zero-power standby backstop: decide what to do after sustained hard 0W.

        Called from _check_energy_finish when instantaneous power is <= 0W. Returns True
        when it transitioned the state (caller must stop the tick), False to keep checking.

        Ladder (all thresholds unchanged from the original inline block):
          * 0W >= 3 min + finish guards met + valid cycle    -> Unemptied (normal finish).
          * 0W >= 5 min + heated + real energy + warm floor  -> Unemptied via safety net (below).
          * 0W >= 5 min otherwise                            -> forced Off (false start / ghost Running).

        The safety net (2026-08-11): a cycle that demonstrably heated water
        (observed_heating) and consumed at least min_energy_kwh is a REAL wash even when
        the finish-time guards never opened - a wrong-long guard bar (eco 199 min frozen
        over an actual ~180 min run -> needing 92% = 183 min) blocked them right up to
        this point. Forcing Off here ended 12 real washes silently since 2026-03: no
        announcement, no Unemptied on the dashboard, no learning record. Publishing
        Unemptied instead is safe because the existing protections still hold: the
        finish_min_run_minutes_warm floor (100 min) and 5 minutes of hard 0W (anti-crease
        tumbles reset the zero-power clock, so a mid-cycle soak never gets here)."""
        if self._zero_power_since is None:
            self._zero_power_since = now
        zero_min = (now - self._zero_power_since).total_seconds() / 60
        if zero_min < 3.0:
            return False
        run_min = (now - self.start_time).total_seconds() / 60 if self.start_time else 0
        guard_dur = self._get_guard_duration(tick_prog, tick_temp, tick_class)
        if self._meets_finish_time_guards(run_min, guard_dur or 0):
            self.log(
                f"Standby backstop: power 0W for {zero_min:.1f}min - machine is off",
                level="INFO",
            )
            if self._is_valid_completed_cycle():
                self._transition_to_unemptied()
                return True
        else:
            self.log(
                f"Standby backstop: 0W for {zero_min:.1f}min but finish time guards not met (run {run_min:.0f}min) - skipping",
                level="DEBUG",
            )
        # Invalid cycle (e.g. false-start or ghost Running state) with sustained
        # zero power - machine is clearly off, go directly to Off.
        if zero_min >= 5.0:
            energy_used = self._get_energy_used()
            if (
                self.observed_heating
                and energy_used >= self.min_energy_kwh
                and run_min >= self.finish_min_run_minutes_warm
            ):
                self.log(
                    f"Standby backstop net: 0W for {zero_min:.1f}min on a heated cycle "
                    f"(run {run_min:.0f}min, energy {energy_used:.2f}kWh) with finish guards "
                    f"never met (guard {guard_dur or 0:.0f}min) - publishing Unemptied instead "
                    f"of silently forcing Off",
                    level="WARNING",
                )
                self._pending_end_reason = "standby_backstop"
                self._transition_to_unemptied()
                return True
            self.log(
                f"Standby backstop: cycle invalid + 0W for {zero_min:.1f}min - forcing Off",
                level="WARNING",
            )
            self._transition_to_off("Standby backstop: invalid cycle with sustained zero power")
            return True
        self.log("Standby backstop but cycle validation failed - keep checking", level="WARNING")
        return False

    def _check_energy_finish(self, kwargs):
        """Check if energy consumption has stopped (cycle finished)."""
        current_state = self.get_state(self.state_entity)

        if current_state != "Running":
            self.energy_check_timer = None
            return

        self._maybe_handle_delayed_start()

        # Compute programme classification once per tick to avoid redundant get_state calls.
        _tick_prog, _tick_temp = self._classify_programme()
        _pred_prog, _pred_temp = self._classify_programme(energy_signature_only=True)
        _tick_class = (_tick_prog, _tick_temp)
        # Track classification stability + evolve the guard bar BEFORE any finish path of this
        # tick consults _get_guard_duration (freeze/raise/lower - see _update_guard_bar).
        self._note_live_classification(_tick_prog, _tick_temp)
        self._update_guard_bar(_tick_prog, _tick_temp)

        run_min = (self._now_utc() - self.start_time).total_seconds() / 60 if self.start_time else 0
        guard_dur = self._get_guard_duration(_tick_prog, _tick_temp, _tick_class)
        self._refresh_tail_pulse_tracking()
        self._update_tail_pattern_lock()
        # If in FinishingTail, only finish via tail-pulse timeout (same as anti-crease / energy paths).
        if self.in_finishing_tail and self._try_finish_via_standby(run_min, guard_dur, _tick_prog, _tick_temp, _tick_class):
            return

        # --- Duration tripwire ---
        # Past max duration + low power: enter FinishingTail only - do not bypass tail-pulse timeout.
        if self.start_time:
            run_min = (self._now_utc() - self.start_time).total_seconds() / 60
            max_dur = self._programme_max_duration_minutes(classification=_tick_class)
            past_max = run_min >= max_dur
            meets_guards = self._meets_finish_time_guards(run_min, guard_dur or 0)
            if past_max and meets_guards:
                current_power = self._get_current_power()
                if current_power < self.significant_w:
                    self.log(
                        f"Duration tripwire: {run_min:.0f}min >= {max_dur}min max for '{_tick_prog}' "
                        f"(finish guards met), power {current_power:.1f}W - entering FinishingTail (tail-pulse timeout required)",
                        level="INFO",
                    )
                    if not self.in_finishing_tail:
                        self.in_finishing_tail = True
                        self.in_finishing_tail_entered_at = self._now_utc()
                        self.last_tail_pulse_at = self._get_last_tail_pulse_time() or self._now_utc()
                    if current_power > self._tail_pulse_reset_threshold_watts():
                        self.last_tail_pulse_at = self._now_utc()
                    self._refresh_tail_pulse_tracking()
                    self._update_tail_pattern_lock()
                    if self._try_finish_via_standby(run_min, guard_dur, _tick_prog, _tick_temp, _tick_class):
                        return
                    self.log("Duration tripwire: waiting for tail-pulse timeout before Unemptied", level="DEBUG")
        # Start time cannot be before the last door close (except AddLoad first 10 min).
        # Enforces: UI must not show a start time from before the user last closed the door.
        # Only clamp when the gap is >= pause_window (10 min), so AddLoad door close doesn't change start_time.
        if (
            self.start_time
            and self.last_door_closed_trusted
            and self.last_door_closed_at
            and self.start_time < self.last_door_closed_at
        ):
            gap_seconds = (self.last_door_closed_at - self.start_time).total_seconds()
            if gap_seconds >= self.pause_window_minutes * 60:
                self.log(
                    f"Clamping start_time to last door close for display "
                    f"(was {self._strftime_local(self.start_time)}, now {self._strftime_local(self.last_door_closed_at)})",
                    level="INFO",
                )
                self.start_time = self.last_door_closed_at
        # Start time cannot be after the last trusted door close (cycle starts when door closes).
        # Fixes wrong start_time from false-finish recovery (e.g. 12:09 when real start was 10:00).
        last_door = self.last_door_closed_at if self.last_door_closed_trusted else None
        if not last_door:
            try:
                if self._attr_bool_true(
                    self.get_state(self.state_entity, attribute="last_door_closed_trusted")
                ):
                    ld_str = self.get_state(self.state_entity, attribute="last_door_closed_at")
                    if ld_str:
                        last_door = _parse_utc(ld_str)
                        if last_door:
                            self.last_door_closed_at = last_door
                            self.last_door_closed_trusted = True
            except (TypeError, ValueError, AttributeError):
                pass
        if (self.start_time and last_door and
                self.start_time > last_door and not self._delayed_start_trimmed):
            gap_seconds = (self.start_time - last_door).total_seconds()
            if gap_seconds >= 60:
                self.log(
                    f"Correcting start_time: was {self._strftime_local(self.start_time)} (after door close) "
                    f"-> {self._strftime_local(last_door)}",
                    level="INFO",
                )
                self.start_time = last_door
                self._start_time_source = "door_close_trusted"
                self._push_corrected_start_time_to_entity()
        # Fallback when last_door_closed_at is missing (e.g. lost during bad recovery): infer from
        # state entity history - when did we first go to Running? That's the cycle start.
        elif self.start_time and not last_door:
            now_utc = self._now_utc()
            if self._last_infer_start_attempt is None or (now_utc - self._last_infer_start_attempt).total_seconds() >= 300:
                self._last_infer_start_attempt = now_utc
                inferred = self._infer_start_from_state_history()
            else:
                inferred = None
            if inferred and inferred < self.start_time:
                gap_min = (self.start_time - inferred).total_seconds() / 60
                if gap_min >= 15:
                    self.log(
                        f"Correcting start_time from state history: was {self._strftime_local(self.start_time)} "
                        f"-> {self._strftime_local(inferred)} (no last_door_closed_at)",
                        level="INFO",
                    )
                    self.start_time = inferred
                    self.last_door_closed_at = inferred
                    self.last_door_closed_trusted = False
                    self._start_time_source = "state_history"
                    self._push_corrected_start_time_to_entity()
        # Also clamp to last_off_at: never show a start time from before we last went Off (fixes second cycle showing first cycle start).
        try:
            last_off_str = self.get_state(self.state_entity, attribute="last_off_at")
            if last_off_str and self.start_time:
                last_off = _parse_utc(last_off_str)
                if last_off and self.start_time < last_off:
                    gap_seconds = (last_off - self.start_time).total_seconds()
                    if gap_seconds >= self.pause_window_minutes * 60:
                        self.log(
                            f"Clamping start_time to after last Off for display "
                            f"(was {self._strftime_local(self.start_time)}, now {self._strftime_local(last_off)})",
                            level="INFO",
                        )
                        self.start_time = last_off
        except (TypeError, ValueError, AttributeError):
            pass

        # If we still have a start time before "when we went to Running", use entity's last_changed as cycle start.
        # This fixes cycle 2 showing cycle 1's start when last_off_at was missing or we restored before it was set.
        # Skip when we have a trusted door time; last_changed can be from recovery (Unemptied->Running).
        try:
            full = self.get_state(self.state_entity, attribute="all")
            if full and self.start_time and not self.last_door_closed_trusted:
                last_changed_str = (full.get("last_changed") or full.get("last_updated")) if isinstance(full, dict) else None
                if not last_changed_str and isinstance(full, dict) and "attributes" in full:
                    attrs_dict = full.get("attributes") or {}
                    last_changed_str = attrs_dict.get("last_changed") or attrs_dict.get("last_updated")
                if last_changed_str:
                    last_changed_dt = _parse_utc(str(last_changed_str))
                    if last_changed_dt and self.start_time < last_changed_dt:
                        gap_seconds = (last_changed_dt - self.start_time).total_seconds()
                        if gap_seconds >= self.pause_window_minutes * 60:
                            # After an HA restart erases and recreates state_entity, its
                            # last_changed is OUR recreation, not a real cycle-2 transition -
                            # do not let it masquerade as one. Bounded window (not an open-ended
                            # >=): a genuine cycle-2 start hours after the restart must still
                            # fall through to the rank gate below, not be silenced forever by a
                            # stamp that is never cleared on its own.
                            if (self._entity_recreated_at is not None
                                    and abs((last_changed_dt - self._entity_recreated_at).total_seconds()) <= 5):
                                self.log(
                                    f"Ignoring entity last_changed {self._strftime_local(last_changed_dt)} "
                                    f"- our own post-restart recreation, not a cycle-2 start",
                                    level="DEBUG",
                                )
                            elif self._start_time_rank() >= 6:
                                self.log(
                                    f"Correcting start_time to entity last_changed (cycle 2 start) "
                                    f"(was {self._strftime_local(self.start_time)}, now {self._strftime_local(last_changed_dt)})",
                                    level="INFO",
                                )
                                self.start_time = last_changed_dt
                                self._start_time_source = "entity_last_changed"
        except (TypeError, ValueError, AttributeError):
            pass

        # --- Update live programme classification + ETA on state entity ---
        new_prog, new_temp = _tick_prog, _tick_temp
        if new_prog != self.detected_programme or new_temp != self.detected_temperature:
            self.detected_programme = new_prog
            self.detected_temperature = new_temp
            profile = self._get_profile(new_prog, new_temp)
            temp_str = f" {new_temp}" if new_temp else ""
            self.log(
                f"Programme classified as '{new_prog}'{self._log_safe(temp_str)} ({self._log_safe(profile.get('label', new_prog))}, "
                f"heating bursts: {self.heating_phase_count}, "
                f"max power: {self.max_power_seen:.0f}W)",
                level="INFO",
            )
        # Guard bar upkeep happens in _update_guard_bar (called at the top of this tick):
        # seeded at the first confident classification so mid-cycle selector changes can
        # never weaken the too-early guard, raised freely, lowered only after energy
        # disproof + stability. This second call is belt-and-braces for the restore case
        # the old freeze handled here: restore pre-set detected_programme to the same
        # value (no change event) but expected_dur_at_start was lost.
        if new_prog != "unknown" and self.expected_dur_at_start is None:
            self._update_guard_bar(new_prog, new_temp)

        # ETA: use selector only when user actually confirmed (_on_confirm_changed set the flag).
        # Do not trust the selector alone - it may be stale or out of sync with Auto.
        eta_prog, eta_temp = new_prog, new_temp
        if self.confirm_entity and self.programme_confirmed_by_user:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    eta_prog = self._LABEL_TO_KEY.get(label, new_prog) or new_prog
                    eta_temp = self._read_temperature_selector() or new_temp
            except Exception:
                pass
        profile = self._get_profile(new_prog, new_temp)
        # ETA display: when user has selected a programme, use that duration so countdown matches
        # what they chose. When on Auto, use expected_dur_at_start (from classification) so we don't
        # show a wrong long countdown from an early wrong classification.
        user_has_selected = bool(
            self.programme_confirmed_by_user
            and self.confirm_entity
            and (self.get_state(self.confirm_entity) or "").strip() not in ("", "Auto (unconfirmed)", "unknown", "unavailable")
        )
        if user_has_selected:
            effective_dur = self._get_programme_duration(eta_prog, eta_temp, use_learned=False)
        else:
            effective_dur = (
                self.expected_dur_at_start
                if self.expected_dur_at_start is not None
                else self._get_programme_duration(eta_prog, eta_temp, use_learned=False)
            )
        # Eco/strygelet ambiguity blend when < 130 min (only when on Auto - if user selected ECO, use ECO duration)
        if eta_prog == "eco" and new_prog == "eco" and not self.programme_confirmed_by_user and not user_has_selected:
            run_min = (self._now_utc() - self.start_time).total_seconds() / 60
            if run_min < 130:
                strygelet_dur = self._get_programme_duration("strygelet", None, use_learned=False)
                eco_dur = self._get_programme_duration("eco", None, use_learned=False)
                energy_used = self._get_energy_used()
                if run_min >= 60 and energy_used < 0.52:
                    effective_dur = strygelet_dur
                else:
                    blend = min(1.0, run_min / 130.0)
                    effective_dur = round(strygelet_dur + blend * (eco_dur - strygelet_dur))
        # Merge new attrs into existing HA state so persisted fields
        # (programme_confirmed_by_user, programme_confirmed_by, last_off_at, etc.)
        # survive the periodic update instead of being silently wiped every tick.
        try:
            full = self.get_state(self.state_entity, attribute="all") or {}
            attrs: dict = dict((full.get("attributes") or {}))
        except Exception:
            attrs = {}
        # Predicted = energy/signature only; cleared while user has confirmed a programme (does not touch input_select).
        if self.programme_confirmed_by_user:
            pred_attrs = {
                "predicted_programme": "",
                "predicted_programme_label": "",
                "predicted_temperature": "",
            }
        else:
            pred_key = _pred_prog if _pred_prog and _pred_prog != "unknown" else ""
            pred_prof = self._get_profile(_pred_prog, _pred_temp) if pred_key else {}
            pred_attrs = {
                "predicted_programme": pred_key,
                "predicted_programme_label": (pred_prof.get("label") or pred_key) if pred_key else "",
                "predicted_temperature": (_pred_temp or "") if pred_key else "",
            }
        attrs.update({
            "detected_programme": new_prog,
            "detected_temperature": new_temp or "",
            "programme_label": profile.get("label", new_prog),
            "cycle_complete": False,
            "run_time_minutes": None,
            "energy_used": round(self._get_energy_used(), 3),
            "end_reason": "",
            "idle_min": None,
            "heating_bursts": self.heating_phase_count,
            "max_power_w": round(self.max_power_seen, 0),
            "delayed_start_trimmed": bool(self._delayed_start_trimmed),
            "delayed_start_waiting": bool(self._delay_waiting),
            **pred_attrs,
        })
        if self.start_time:
            attrs["cycle_start_time"] = self._format_utc(self.start_time)
            attrs["cycle_start_time_local"] = self._format_local(self.start_time)
            attrs["started_at_display"] = self.start_time.astimezone(self._local_tz()).strftime("%H:%M")
            elapsed_min = (self._now_utc() - self.start_time).total_seconds() / 60
            remaining = max(0, round(effective_dur - elapsed_min))
            est_end = self.start_time + timedelta(minutes=effective_dur)
            attrs["estimated_remaining_min"] = remaining
            attrs["estimated_end_time"] = est_end.astimezone(self._local_tz()).strftime("%H:%M")
            attrs["elapsed_minutes"] = round(elapsed_min, 1)
            attrs["progress_pct"] = min(100, max(0, round(100 * elapsed_min / effective_dur))) if effective_dur else 0
            attrs["programme_duration_min"] = effective_dur
            if self._delay_waiting and self.delayed_start_show_waiting:
                # Suspected delayed-start wait: blank ETA/progress instead of showing a bogus
                # countdown from the (not yet corrected) start_time. cycle_start_time is left
                # as-is; delayed_start_waiting (set above) tells the UI why ETA is blank.
                attrs["estimated_remaining_min"] = None
                attrs["estimated_end_time"] = ""
                attrs["progress_pct"] = 0
                attrs["programme_duration_min"] = None
        if self.energy_start is not None:
            attrs["energy_at_start"] = self.energy_start
        if self.last_high_energy_at is not None:
            attrs["last_high_energy_at"] = self._format_local(self.last_high_energy_at)
        if self.last_door_closed_at:
            attrs["last_door_closed_at"] = self._format_local(self.last_door_closed_at)
        attrs["last_door_closed_trusted"] = bool(self.last_door_closed_trusted)
        # Ensure persisted flags stay present
        attrs["programme_confirmed_by_user"] = bool(self.programme_confirmed_by_user)
        attrs["programme_confirmed_by"] = self.confirmed_by_username or ""
        attrs["expected_dur_at_start"] = self.expected_dur_at_start if self.expected_dur_at_start is not None else ""
        attrs["expected_dur_key"] = self._guard_bar_key_str()
        attrs["session_cost_kr"] = round(self._session_cost_kr, 2)
        # cycle_complete/heating_bursts/progress_pct/estimated_remaining_min/last_door_closed_trusted/
        # programme_confirmed_by_user/delayed_start_trimmed/delayed_start_waiting can all legitimately
        # be False/0 on a normal mid-cycle tick (still running, pre-heating, near start/end of
        # countdown, Auto mode, no trusted door-close, no delayed start suspected) --
        # AppDaemon 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
        self._set_state_entity( state="Running", attributes=attrs)

        # --- Finish precedence: (1) user_cycle_end, (2) anti_crease_pattern, (3) low_power_detected ---
        # Anti-crease: use raw power history as primary signal (independent from energy bookkeeping).
        now = self._now_utc()
        self._in_finish_debug_window = False
        if self.start_time:
            run_min = (now - self.start_time).total_seconds() / 60
            guard_dur = self._get_guard_duration(_tick_prog, _tick_temp, _tick_class)
            # Debug logging when in final window (finish_debug_window_minutes before expected end or past)
            in_finish_debug_window = guard_dur and (
                run_min >= guard_dur - self.finish_debug_window_minutes or run_min >= guard_dur
            )
            if in_finish_debug_window:
                tail_ok, tail_mean, tail_std, tail_peak = self._detect_anti_crease_pattern()
                recent_activity = self._recent_true_activity_block()
                idle_min = (now - self.last_high_energy_at).total_seconds() / 60 if self.last_high_energy_at else None
                self.log(
                    f"Finish debug: run_min={run_min:.1f} expected_dur={guard_dur:.0f} "
                    f"anti_crease_candidate={tail_ok} tail_mean={tail_mean} tail_std={tail_std} tail_peak={tail_peak} "
                    f"recent_true_activity_block={recent_activity} idle_min={idle_min} "
                    f"last_high_energy_at={self._strftime_local(self.last_high_energy_at) if self.last_high_energy_at else None}",
                    level="DEBUG",
                )
            self._in_finish_debug_window = in_finish_debug_window  # For energy-block debug log
            # (1) user_cycle_end is handled via cycle_ended_at_entity in _correct_duration when we transition.
            # (2) anti_crease_pattern: disable when programme known and supports_anti_crease is False (e.g. Uld per manual).
            profile_tick = self._get_profile(_tick_prog, _tick_temp)
            supports_anti_crease = profile_tick.get("supports_anti_crease", True) if profile_tick else True
            if _tick_prog == "uld":
                supports_anti_crease = False  # Manual: Uld is the exception
            if supports_anti_crease:
                # Stricter finish-time guard: fraction of expected + min runtime (stops false announce when guard_dur is wrong).
                if not self._meets_finish_time_guards(run_min, guard_dur or 0):
                    if in_finish_debug_window:
                        min_run = self._get_finish_min_run_minutes()
                        pct = (run_min / guard_dur) * 100 if guard_dur else 0
                        self.log(
                            f"Anti-crease candidate but finish guards not met: run {run_min:.0f}min "
                            f"(need {self.finish_guard_fraction*100:.0f}% of {guard_dur:.0f}min and >= {min_run:.0f}min) - blocking",
                            level="DEBUG",
                        )
                elif self._meets_finish_time_guards(run_min, guard_dur or 0) and self._is_post_end_tail_window(run_min, guard_dur, _tick_prog) and not self._recent_true_activity_block():
                    tail_ok, tail_mean, tail_std, tail_peak = self._detect_anti_crease_pattern()
                    if tail_ok:
                        if (
                            self.anti_crease_announce_past_expected
                            and guard_dur
                            and run_min >= guard_dur  # STRICTLY past expected end, not merely near it
                            and self._is_valid_completed_cycle()
                        ):
                            # Past expected end with a confirmed anti-crease pattern: the pattern IS
                            # the end signal here (mirrors the dryer's keep-fresh transition, ~4 min
                            # detections) - skip FinishingTail's slower tail-pulse-timeout wait and
                            # announce now. _transition_to_unemptied() still runs
                            # _power_looks_like_cycle_end() for this end_reason (that gate is only
                            # skipped for tail_to_standby/tail_pattern_break) so the mid-cycle-rinse
                            # sanity check is not bypassed - just not duplicated here.
                            self._pending_end_reason = "anti_crease_pattern"
                            self.log(
                                f"Anti-crease pattern past expected end (run={run_min:.0f}min >= expected={guard_dur:.0f}min, "
                                f"tail mean={tail_mean:.1f}W std={tail_std:.1f}W peak={tail_peak:.1f}W) - announcing immediately",
                                level="INFO",
                            )
                            self._transition_to_unemptied()
                            return
                        if not self.in_finishing_tail:
                            self.in_finishing_tail = True
                            self.in_finishing_tail_entered_at = now
                            self.last_tail_pulse_at = self._get_last_tail_pulse_time() or now
                            self.log(
                                f"FinishingTail entered (anti-crease pattern, tail mean={tail_mean:.1f}W std={tail_std:.1f}W peak={tail_peak:.1f}W) - will announce when no pulse >{self.finishing_tail_pulse_reset_watts:.0f}W for {self.tail_pulse_timeout_seconds:.0f}s",
                                level="INFO",
                            )
                        if self._try_finish_via_standby(run_min, guard_dur, _tick_prog, _tick_temp, _tick_class):
                            return
                        return  # Stay in Running until standby detected
                # If already in FinishingTail (e.g. from energy path), try standby transition
                if self.in_finishing_tail and self._try_finish_via_standby(run_min, guard_dur, _tick_prog, _tick_temp, _tick_class):
                    return

        # Do not write classified programme into input_select.washer_confirmed_programme.
        # That dropdown is only for *user* intent; ETA / guards use detected_programme + expected_dur_at_start
        # on the state entity when the user leaves "Auto (unconfirmed)". Mirroring prediction into the selector
        # overwrote manual Auto and made the next tick treat ECO as "user confirmed".

        try:
            current_energy = self.get_state(self.energy_sensor)
            if current_energy is None or current_energy in ["unknown", "unavailable"]:
                # Energy unavailable - reschedule check
                self.energy_check_timer = self.run_in(self._check_energy_finish, self.energy_check_interval)
                return
            
            current_energy_value = float(current_energy)
            now = self._now_utc()

            # Settled per-cycle cost: meter the spot price against this tick's energy delta
            # (mirrors SmartCooling._track_session_cost). Reuses current_energy_value already
            # read above - no extra get_state for energy. Tracked in dedicated vars (see
            # initialize()) so unrelated energy-buffer resets elsewhere never corrupt it.
            if self.track_cycle_cost:
                if self._cost_prev_energy_kwh is None:
                    # First reading since cycle start (or after a restart) - establish the
                    # baseline only; nothing to charge for yet.
                    self._cost_prev_energy_kwh = current_energy_value
                else:
                    cost_delta_kwh = current_energy_value - self._cost_prev_energy_kwh
                    if cost_delta_kwh < 0:
                        cost_delta_kwh = 0.0  # meter reset - never subtract
                    try:
                        price_now = float(self.get_state(self.price_entity))
                    except (TypeError, ValueError):
                        price_now = self.price_fallback_kr
                    self._session_cost_kr += cost_delta_kwh * price_now
                    self._cost_prev_energy_kwh = current_energy_value

            # Standby backstop: if instantaneous power is 0W for 3+ minutes,
            # the machine is completely off - force finish regardless of the
            # rolling energy window (which lags due to REF_WINDOW_S).
            current_power = self._get_current_power()
            if current_power <= 0.0:
                if self._standby_backstop_tick(now, _tick_prog, _tick_temp, _tick_class):
                    return
            else:
                # Anti-crease tumbles are post-end activity, not cycle activity: once past
                # expected end with the pattern currently confirmed, a brief tumble (< 120W)
                # must not reset the zero-power clock, or every tumble would keep pushing the
                # 3/5-minute thresholds back out - the exact lag the rest of this fix removes.
                tumble_tolerated = False
                if self.anti_crease_announce_past_expected and current_power < 120.0 and self.start_time:
                    tumble_run_min = (now - self.start_time).total_seconds() / 60
                    tumble_guard_dur = self._get_guard_duration(_tick_prog, _tick_temp, _tick_class)
                    if tumble_guard_dur and tumble_run_min >= tumble_guard_dur:
                        tail_ok, tail_mean, tail_std, tail_peak = self._detect_anti_crease_pattern()
                        tumble_tolerated = tail_ok
                if not tumble_tolerated:
                    self._zero_power_since = None

            # Rolling-buffer implied-watts calculation.
            #
            # Problem with comparing consecutive 30s readings: the Zigbee energy sensor
            # updates only every ~60s. When we check at 30s intervals, every other check
            # sees zero delta (sensor hasn't reported yet) -> spurious 0W -> false "stable"
            # starts during active washing -> counter resets constantly -> 15+ min to confirm.
            #
            # Fix: always compare to the most-recent buffer entry that is ≥ REF_WINDOW_S old.
            # This guarantees the delta spans at least one full sensor update cycle, so we
            # never see the 0W aliasing artifact.
            REF_WINDOW_S = 90  # Must exceed the sensor's ~60s update interval

            self.energy_buffer.append((now, current_energy_value))
            cutoff = now - timedelta(minutes=20)
            self.energy_buffer = [(t, e) for t, e in self.energy_buffer if t >= cutoff]

            # Walk the (chronological) buffer to find the most-recent entry ≥ REF_WINDOW_S old
            ref_time, ref_energy = None, None
            for t, e in self.energy_buffer:
                if (now - t).total_seconds() >= REF_WINDOW_S:
                    ref_time, ref_energy = t, e  # keep updating -> most-recent qualifying point

            if ref_time is None:
                # Buffer still warming up (< REF_WINDOW_S since cycle start) - reschedule
                self.energy_check_timer = self.run_in(self._check_energy_finish, self.energy_check_interval)
                return

            delta_s = (now - ref_time).total_seconds()
            delta_kwh = current_energy_value - ref_energy
            if delta_kwh < -0.001:
                # Energy meter reset or sensor glitch - negative delta looks "super idle"
                # and would immediately trigger a false finish. Reset the buffer and restart.
                self.log(
                    f"Energy delta negative ({delta_kwh:.4f} kWh) - sensor reset detected, "
                    f"resetting energy buffer",
                    level="WARNING",
                )
                self.energy_buffer = [(now, current_energy_value)]
                self.last_high_energy_at = now
                self.energy_stable_start_time = None
                self.energy_check_timer = self.run_in(self._check_energy_finish, self.energy_check_interval)
                return
            delta_kwh = max(0.0, delta_kwh)
            avg_watts = (delta_kwh * 1000) / (delta_s / 3600)

            if getattr(self, "_in_finish_debug_window", False):
                idle_min_debug = (now - self.last_high_energy_at).total_seconds() / 60 if self.last_high_energy_at else None
                valid = self._is_valid_completed_cycle()
                self.log(
                    f"Finish debug (energy): avg_watts={avg_watts:.1f} current_power={current_power:.1f} "
                    f"idle_minutes={idle_min_debug} valid_for_finish={valid}",
                    level="DEBUG",
                )

            if avg_watts > self.energy_active_watts:
                # Main cycle activity (heating/spin) - record the last time we saw it.
                if (self.last_high_energy_at is None or
                        (now - self.last_high_energy_at).total_seconds() > 60):
                    self.log(f"Energy active ({avg_watts:.2f}W) - cycle still running", level="DEBUG")
                self.last_high_energy_at = now
                self.energy_stable_start_time = None
                self.finish_confirmed = False
            elif avg_watts <= self.post_cycle_idle_watts:
                # Idle or post-cycle slow spin (motor at 30-80W) - count toward finish.
                if self.last_high_energy_at is not None:
                    idle_minutes = (now - self.last_high_energy_at).total_seconds() / 60
                    if self.energy_stable_start_time is None:
                        self.energy_stable_start_time = now
                        self.log(
                            f"Energy stable ({avg_watts:.2f}W avg over {delta_s:.0f}s window, "
                            f"last active {idle_minutes:.1f}min ago)",
                            level="DEBUG",
                        )

                    effective_minutes = self._effective_stable_minutes(classification=_tick_class)
                    use_pattern = self._detect_post_cycle_slow_spin_pattern()
                    required_minutes = self.post_cycle_pattern_minutes if use_pattern else effective_minutes
                    # When run is near or past expected duration, use shorter window so we declare finish before door opens (~10:52 not 11:05).
                    # Prefer user-confirmed programme for expected_dur - avoids wrong "near end" from misclassification.
                    if self.start_time:
                        run_min = (now - self.start_time).total_seconds() / 60
                        expected_dur = None
                        guard_prog, guard_temp = _tick_prog, _tick_temp
                        if self.confirm_entity and self.programme_confirmed_by_user:
                            try:
                                label = self.get_state(self.confirm_entity)
                                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                                    p = self._LABEL_TO_KEY.get(label, "unknown")
                                    if p and p != "unknown":
                                        guard_prog, guard_temp = p, self._read_temperature_selector() if self._programme_has_temperature(p) else None
                            except Exception:
                                pass
                        if guard_prog and guard_prog != "unknown":
                            expected_dur = self._get_programme_duration(guard_prog, guard_temp, use_learned=False)
                            if expected_dur:
                                # In the last hour of expected run, or past 90%: 5 min stable is enough.
                                # Use 90% (not 80%) to avoid false finish from mid-cycle soak (e.g. Bomuld 30°C at 82%).
                                if run_min >= expected_dur - 60 or run_min >= 0.90 * expected_dur:
                                    required_minutes = min(required_minutes, self.finish_stable_minutes_near_end)
                                    if required_minutes < effective_minutes:
                                        self.log(
                                            f"Near end: run {run_min:.0f}min (expected ~{expected_dur}min) -> require only {required_minutes}min stable (finish before door opens)",
                                            level="DEBUG",
                                        )
                                # In the last 30 min of expected run: only 3 min idle (cycle often ends a bit early, e.g. partial load).
                                if run_min >= expected_dur - 30:
                                    required_minutes = min(required_minutes, 3)
                                # Past expected end: cycle may have finished early (e.g. partial load). Require only 2 min idle so we don't stay "Running" long after machine stops (user: cycle ended 13:33, UI still Running).
                                if run_min >= expected_dur:
                                    required_minutes = min(required_minutes, 2)
                                    if required_minutes == 2:
                                        self.log(
                                            f"Past expected end: run {run_min:.0f}min >= {expected_dur}min -> require only 2min idle to finish",
                                            level="DEBUG",
                                        )
                    if idle_minutes >= required_minutes:
                        guard_dur = self._get_guard_duration(_tick_prog, _tick_temp, _tick_class)
                        if not self._meets_finish_time_guards(run_min, guard_dur or 0):
                            min_run = self._get_finish_min_run_minutes()
                            # idle_minutes = time since last high *energy* (main wash activity), not "stable cycle" length
                            msg = (
                                f"Energy idle {idle_minutes:.0f}min (since last main activity) but finish time guards "
                                f"not met: total run {run_min:.0f}min "
                                f"(need >= {min_run:.0f}min and {self.finish_guard_fraction*100:.0f}% of {guard_dur:.0f}min) "
                                f"- blocking false finish"
                            )
                            now = self._now_utc()
                            throttle_s = int(self.args.get("finish_guard_log_interval_s", 600))
                            if (
                                self._last_finish_guard_info_log_at is None
                                or (now - self._last_finish_guard_info_log_at).total_seconds() >= throttle_s
                            ):
                                self._last_finish_guard_info_log_at = now
                                self.log(msg, level="INFO")
                            else:
                                self.log(msg, level="DEBUG")
                        else:
                            current_power = self._get_current_power()
                            if current_power < self.post_cycle_idle_watts:
                                self.finish_confirmed = True
                                if not self.in_finishing_tail:
                                    self.in_finishing_tail = True
                                    self.in_finishing_tail_entered_at = now
                                    self.last_tail_pulse_at = self._get_last_tail_pulse_time() or now
                                    programme_type = "warm" if self.observed_heating else "cold/wool"
                                    self.log(
                                        f"FinishingTail entered (energy stable {idle_minutes:.1f}min, {programme_type}, "
                                        f"power {current_power:.1f}W) - will announce when no pulse >{self.finishing_tail_pulse_reset_watts:.0f}W for {self.tail_pulse_timeout_seconds:.0f}s",
                                        level="INFO",
                                    )
                                if current_power > self._tail_pulse_reset_threshold_watts():
                                    self.last_tail_pulse_at = now
                                if self.get_state(self.state_entity) == "Running":
                                    if self._try_finish_via_standby(run_min, guard_dur, _tick_prog, _tick_temp, _tick_class):
                                        return
                                    self.log("Finish confirmed but tail pulse timeout not yet met - keep checking", level="DEBUG")
                            else:
                                self.log(
                                    f"Energy quiet {idle_minutes:.1f}min but power still high "
                                    f"({current_power:.1f}W >= {self.post_cycle_idle_watts:.0f}W), waiting...",
                                    level="DEBUG",
                                )
                    else:
                        programme_type = "warm" if self.observed_heating else (
                            "cold/wool" if self.start_time and
                            (self._now_utc() - self.start_time).total_seconds() > 600
                            else "unclassified"
                        )
                        self.log(
                            f"Energy stable {idle_minutes:.1f}/{required_minutes}min "
                            f"({programme_type}, avg {avg_watts:.1f}W)",
                            level="DEBUG",
                        )
            else:
                # Between post_cycle_idle_watts and energy_active_watts (e.g. 80-100W).
                # Don't reset last_high_energy_at - the Miele's post-cycle pump spikes
                # (30-40W averaged over 90s) would perpetually delay finish detection.
                # Only reset the stable-start counter so we require a fresh idle period.
                if (self.last_high_energy_at is None or
                        (now - self.last_high_energy_at).total_seconds() > 60):
                    self.log(f"Energy medium ({avg_watts:.2f}W) - resetting stable counter but not idle timer", level="DEBUG")
                self.energy_stable_start_time = None
                self.finish_confirmed = False

            # Schedule next check
            self.energy_check_timer = self.run_in(self._check_energy_finish, self.energy_check_interval)
            
        except (ValueError, TypeError) as e:
            self.log(f"Error checking energy: {e}", level="WARNING")
            self.energy_check_timer = self.run_in(self._check_energy_finish, self.energy_check_interval)

    def _auto_analyze_after_cycle(self, kwargs):
        """Automatically analyze recent cycles after a cycle completes."""
        try:
            self.analyze_recent_cycles(hours_back=24)  # Analyze last 24 hours
        except Exception as e:
            self.log(f"Error in auto-analysis: {e}", level="WARNING")

    def _log_cycle_analysis(self, cycles):
        """Log cycle analysis results."""
        if not cycles:
            self.log("No cycles found in analysis period", level="INFO")
            return
        
        self.log(f"{'='*80}", level="INFO")
        self.log(f"WASHER CYCLE ANALYSIS - {len(cycles)} cycles found", level="INFO")
        self.log(f"{'='*80}", level="INFO")
        
        for i, cycle in enumerate(cycles, 1):
            self.log(f"Cycle {i}:", level="INFO")
            self.log(f"  Start:      {self._strftime_local(cycle['start'], '%Y-%m-%d %H:%M:%S')}", level="INFO")
            self.log(f"  End:        {self._strftime_local(cycle['end'], '%Y-%m-%d %H:%M:%S')}", level="INFO")
            self.log(f"  Duration:   {cycle['duration_minutes']:.1f} minutes ({cycle['duration_minutes']/60:.2f} hours)", level="INFO")
            self.log(f"  Energy:     {cycle['energy_kwh']:.3f} kWh", level="INFO")
            self.log(f"  Avg Power:  {cycle['energy_kwh'] * 1000 / (cycle['duration_minutes'] / 60):.1f} W", level="INFO")
            self.log(f"  End State:  {cycle['end_state']}", level="INFO")
        
        if len(cycles) >= 2:
            self.log(f"\n{'='*80}", level="INFO")
            self.log("COMPARISON", level="INFO")
            self.log(f"{'='*80}", level="INFO")
            
            for i in range(len(cycles) - 1):
                c1, c2 = cycles[i], cycles[i+1]
                self.log(f"Cycle {i+1} vs Cycle {i+2}:", level="INFO")
                self.log(f"  Duration:   {c1['duration_minutes']:.1f} min vs {c2['duration_minutes']:.1f} min (diff: {abs(c1['duration_minutes'] - c2['duration_minutes']):.1f} min)", level="INFO")
                self.log(f"  Energy:     {c1['energy_kwh']:.3f} kWh vs {c2['energy_kwh']:.3f} kWh (diff: {abs(c1['energy_kwh'] - c2['energy_kwh']):.3f} kWh)", level="INFO")
                self.log(f"  Avg Power:  {c1['energy_kwh'] * 1000 / (c1['duration_minutes'] / 60):.1f} W vs {c2['energy_kwh'] * 1000 / (c2['duration_minutes'] / 60):.1f} W", level="INFO")
