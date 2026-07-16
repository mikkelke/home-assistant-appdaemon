"""
DishwasherMonitor - Tracks dishwasher state with power monitoring and program detection.

Appliance: Miele G5050 VI Active
Programs (per manual): QuickPowerWash, Gentle, ECO, Auto, Intensive. Rinse = QuickPowerWash with short=Yes. ECO has "short" (Express) toggle.
- Programme classification from energy + runtime (not power range)
- Full-duration guard before declaring Unemptied (ECO has long 0–3 W drying phase; real end ~4 h with small power blip)
- Feedback file stores all cycles; programme_confirmed_by_human for learning
- Fill window: 74 min (ECO with short=Yes) - door open before = adding dishes
- Start: Off -> Running on power (readings >= start_w), but a start-candidate phase keeps HA in Off
  until samples/peak/idle pattern (or high-W heater signature, or strict sustain) confirms a real run;
  door-close only accelerates the reading counter — it cannot publish Running on a single weak blip.
  After Unemptied / long plug outage -> Error, require door open, door-close arm, or sustained high
  power for start_sustain_seconds_without_door before the candidate can commit — so short plug blips
  cannot start a "new cycle".
- Plug unavailable: brief gaps are ignored; if power stays unavailable/unknown for
  power_unavailable_error_after_seconds -> Error (not Off). Recovery: power returns -> Off; or force_off / force_unemptied.

Normal full wash (every time): Off -> Running -> Unemptied -> Emptied -> Off
    (Power detects finish while door closed -> Unemptied; user opens -> Emptied; closes -> Off.)

States:
    - Off: Idle, ready for next load, or after emptying (door closed)
    - Running: Wash cycle in progress
    - Unemptied: Cycle completed (power dropped), door still closed — waiting to empty
    - Emptied: Door open after cycle complete — user emptying
    - Error: Power plug unavailable too long — confirm plug/HA; use force_off / force_unemptied to clear
    - Paused: Exception only — door opened during Running with high power (adding dishes), or mid-cycle pause
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import os
import time
import yaml
from datetime import datetime, timedelta, timezone


def _parse_utc(s: str):
    """Parse ISO timestamp to timezone-aware UTC datetime."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    if "+" not in s[-7:] and "-" not in s[-7:]:
        s = s + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


class DishwasherMonitor(hass.Hass):
    PROGRAMME_PROFILES = {}

    def _safe_cancel_timer(self, handle):
        """Cancel a timer only if still running (avoids invalid-handle warnings)."""
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
                return True
        except Exception:
            pass
        return False

    _APPLIANCE_UI_STATES = frozenset({"Off", "Running", "Paused", "Unemptied", "Emptied", "Error"})

    def _sync_ui_select(self, state_str):
        """Mirror state string to input_select helper (Lovelace survives HA restart)."""
        sel = getattr(self, "ui_state_select", None)
        if not sel or state_str not in self._APPLIANCE_UI_STATES:
            return
        try:
            self.call_service("input_select/select_option", entity_id=sel, option=state_str)
            # Defer verify so HA can apply; catches failed sync or manual helper edits (no ha-dashboard change needed)
            self.run_in(self._verify_ui_select_mismatch, 2, expected=state_str)
        except Exception as e:
            self.log(f"ui_state_select sync failed ({sel!r} -> {state_str}): {e}", level="DEBUG")

    def _verify_ui_select_mismatch(self, kwargs):
        """Log WARNING if input_select still disagrees with sensor after sync (upstream fix: sync or stop editing helper)."""
        expected = kwargs.get("expected")
        sel = getattr(self, "ui_state_select", None)
        if not expected or not sel:
            return
        try:
            actual = self.get_state(sel)
            sensor_st = self.get_state(self.state_entity)
            if actual and actual != expected:
                self.log(
                    f"State mirror mismatch: {self.state_entity}={sensor_st!r} (authoritative) but {sel}={actual!r} "
                    f"(expected {expected!r}) - fix AppDaemon sync or manual helper edit",
                    level="WARNING",
                )
        except Exception as e:
            self.log(f"ui_state_select verify failed: {e}", level="DEBUG")

    def _set_state_entity(self, **kwargs):
        """Publish to sensor.*_state and sync input_select.* when state changes."""
        self.set_state(self.state_entity, **kwargs)
        st = kwargs.get("state")
        if st is not None:
            self._sync_ui_select(st)

    def initialize(self):
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
        self.confirmed_programme_entity = self.args.get("confirmed_programme_entity")
        self.short_entity = self.args.get("short_entity")
        # When True, reset programme selector to "—" at cycle start so user must confirm each run
        self.reset_programme_selector_on_start = self.args.get("reset_programme_selector_on_start", True)
        self.start_w = float(self.args["start_w"])
        self.stop_w = float(self.args["stop_w"])
        self.run_for = int(self.args["run_for"])
        self.stop_for = int(self.args["stop_for"])

        # Programmes from YAML (replaces programs/power_range)
        self._load_programme_profiles()

        # Feedback and learning
        self.feedback_file = self.args.get("feedback_file") or os.path.join(
            os.path.dirname(__file__), "dishwasher_feedback.json"
        )
        self._learned_durations = {}
        self._load_and_apply_feedback()

        # Cycle validation and fill window
        self.min_cycle_minutes = int(self.args.get("min_cycle_minutes", 74))
        self.min_energy_kwh = float(self.args.get("min_energy_kwh", 0.3))
        self.fill_window_minutes = int(self.args.get("fill_window_minutes", 74))
        self.pause_timeout_minutes = int(self.args.get("pause_timeout_minutes", 5))

        # History correction: threshold for "high power" in energy series (W)
        self.energy_active_watts = float(self.args.get("energy_active_watts", 100.0))

        # Start candidate: HA stays Off/Unemptied until power evidence proves a real start (blocks plug blips / door-arm single-sample false starts).
        self.start_candidate_window_s = int(
            self.args.get("start_validation_window_s", self.args.get("start_candidate_window_s", 180))
        )
        self.start_candidate_min_active_samples = int(
            self.args.get("start_validation_min_active_samples", self.args.get("start_candidate_min_active_samples", 3))
        )
        self.start_candidate_min_peak_w = float(
            self.args.get("start_validation_fast_confirm_w", self.args.get("start_candidate_min_peak_w", 17.0))
        )
        self.start_candidate_extended_active_samples = int(
            self.args.get(
                "start_validation_fast_confirm_samples",
                self.args.get("start_candidate_extended_active_samples", 3),
            )
        )
        self.start_candidate_high_confidence_w = float(
            self.args.get(
                "start_validation_high_confidence_w",
                self.args.get("start_candidate_high_confidence_w", self.energy_active_watts),
            )
        )
        self.start_candidate_idle_w = float(
            self.args.get("start_validation_idle_w", self.args.get("start_candidate_idle_w", self.stop_w))
        )
        self.start_candidate_idle_grace_s = int(self.args.get("start_candidate_idle_grace_s", 25))
        apply_to_recovery = bool(
            self.args.get(
                "start_validation_apply_to_recovery",
                self.args.get("start_candidate_apply_to_unemptied_recovery", True),
            )
        )
        if not apply_to_recovery:
            self.log(
                "start_validation_apply_to_recovery=false is ignored; recovery path is always gated by start validation",
                level="WARNING",
            )
        self.start_candidate_apply_to_unemptied_recovery = True
        # Optional tie-breaker: if >0, allow commit when energy (kWh) rose this much since candidate began,
        # with at least min_active_samples (coarse meter — keep 0 unless you tune from history).
        self.start_candidate_min_energy_delta_kwh = float(
            self.args.get(
                "start_validation_min_energy_kwh",
                self.args.get("start_candidate_min_energy_delta_kwh", 0.0),
            )
        )
        self._start_candidate_active = False
        self._start_candidate_source = None  # "off" | "unemptied_recovery"
        self._start_candidate_active_samples = 0
        self._start_candidate_max_w = 0.0
        self._start_candidate_began_at = None
        self._start_candidate_energy_at_start = None  # kWh snapshot when candidate began
        self._start_candidate_window_timer = None
        self._start_candidate_idle_timer = None

        # Safety watchdogs - prevent stuck states
        self.max_running_hours = float(self.args.get("max_running_hours", 5))
        self.unemptied_timeout_hours = float(self.args.get("unemptied_timeout_hours", 0))  # 0 = disabled
        self.emptied_timeout_minutes = int(self.args.get("emptied_timeout_minutes", 5))
        self.min_emptying_seconds = int(self.args.get("min_emptying_seconds", 45))

        # Power readings buffer and state stability
        self.power_readings = []
        self.pattern_window = 10
        self.high_power_counter = 0
        self.low_power_counter = 0
        self.high_power_threshold = int(self.args.get("high_power_threshold", 2))  # 2 = enter Running sooner so programme selector appears quickly
        self.low_power_threshold = 5
        self.cooling_period = 300

        # Door-close fast start (power still gates Running): factual close vs temporary arm
        self.door_close_fast_start_window_s = int(self.args.get("door_close_fast_start_window_s", 900))
        self.last_door_closed_at = None  # timezone-aware UTC
        self.door_fast_start_armed_until = None
        self.pending_start_used_fast_path = False

        # After a wash ends, require door interaction OR sustained high power before Off->Running (blocks plug blips)
        self.start_sustain_seconds_without_door = int(self.args.get("start_sustain_seconds_without_door", 120))
        self._strict_start_until_door_or_sustain = False  # True after Unemptied or abnormal Off while wash had run
        self._sustain_start_begin = None  # timezone-aware UTC: first moment power >= start_w while gated Off
        # Plug unavailable: tolerate short gaps; after this many seconds -> Error (not Off)
        self.power_unavailable_error_after_seconds = int(self.args.get("power_unavailable_error_after_seconds", 180))
        self.power_unavailable_error_timer = None
        # A dead plug is maintenance Mikkel must act on -> page the phone, one push per
        # outage + all-clear on recovery (gw2000a_watchdog policy; the house feed stays
        # out of it - dead sensors are not house behavior).
        self.notify_target = self.args.get("notify_target", ["mikkel"])
        self._plug_error_pushed = False

        # State tracking
        self.program_timer = None
        self.low_power_timer = None
        self.start_time = None
        self.energy_start = None
        self.last_state_change = None
        self.notification_sent = False
        self.poll_timer = None
        self.classify_timer = None
        self.expected_dur_at_start = None
        self.detected_programme = "unknown"
        self.detected_short = False  # ECO with short=Yes inferred from energy+runtime
        self.detected_quick_short = False  # QuickPowerWash with short=Yes (rinse/salt cycle)
        self.max_power_w = 0.0
        self._last_high_power_time = None  # Last time power was high (e.g. >50 W); used to detect drying phase

        # Pause state tracking
        self.pause_timer = None
        self.pause_finish_timer = None
        self.pause_from_low_power = False
        self.door_opened_time = None
        self.door_opened_during_cycle = False

        # Watchdog timers
        self.running_watchdog_timer = None
        self.unemptied_watchdog_timer = None
        self.emptied_timeout_timer = None
        self.emptied_at = None  # When we entered Emptied (for min_emptying_seconds check)

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

        # Restore previous state
        existing = self.get_state(self.state_entity)
        valid_states = ("Running", "Unemptied", "Paused", "Emptied", "Error")
        self.state = existing if existing in valid_states else "Off"
        self._set_state_entity( state=self.state)
        if self.state == "Unemptied":
            self._strict_start_until_door_or_sustain = True

        if self.state == "Error":
            ps = self.get_state(self.power_sensor)
            if ps not in ("unknown", "unavailable", None):
                self._transition_to_off("Persisted Error cleared on startup (power sensor OK)", force=True)
            else:
                self.log(
                    "Startup: Error persists while power sensor still unavailable — fix plug/HA or dishwasher_force_off",
                    level="WARNING",
                )

        # Restore cycle tracking for Running/Paused so duration, energy, and pause-exit logic work after AD restart
        if self.state in ("Running", "Paused"):
            self._restore_cycle_tracking_from_entity()
            if self.state == "Running":
                if self.start_time and not self.poll_timer:
                    self.poll_timer = self.run_in(self._poll_power, 60)
                if self.start_time and not self.classify_timer:
                    self.classify_timer = self.run_in(self._tick_classify, 10)
            elif self.state == "Paused" and self.start_time and not self.poll_timer:
                self.poll_timer = self.run_in(self._poll_power, 60)

        # Listen for power changes
        self.listen_state(self._power_changed, self.power_sensor)

        # Listen for door opening AND closing
        self.listen_state(self._door_state_changed, self.door_sensor)

        # Listen for unavailable states
        self.listen_state(self._handle_unavailable, self.state_entity, new="unavailable")
        self.listen_state(self._handle_unavailable, self.power_sensor, new="unavailable")
        self.listen_state(self._handle_unavailable, self.power_sensor, new="unknown")

        # Listen for force-off / force-unemptied (e.g. from script or Developer Tools -> Events)
        self.listen_event(self._handle_force_off, "dishwasher_force_off")
        self.listen_event(self._handle_force_unemptied, "dishwasher_force_unemptied")
        self.listen_event(self._handle_force_emptied, "dishwasher_force_emptied")

        # Bootstrap evaluation
        current_power_state = self.get_state(self.power_sensor)
        if current_power_state not in ["unknown", "unavailable"]:
            try:
                current_watts = float(current_power_state or 0)
                self._power_changed(self.power_sensor, None, None, current_watts, {})
            except (ValueError, TypeError):
                self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
        elif self.state not in ("Error",):
            self._begin_power_unavailable_grace(current_power_state or "unavailable")

        # Self-heal: if we're still Running but power is 0 and we're way past expected duration, force Unemptied now
        if self.state == "Running" and self.start_time and current_power_state not in ["unknown", "unavailable"]:
            try:
                watts = float(current_power_state or 0)
                if watts <= self.stop_w:
                    run_min = (self._now_utc() - self.start_time).total_seconds() / 60
                    if run_min >= self.min_cycle_minutes and run_min >= 60:  # at least 1 hour "running" with 0W
                        energy_used = self._get_energy_used()
                        if energy_used >= self.min_energy_kwh:
                            classified = self._classify_programme()
                            display_prog = self._get_programme_for_display()
                            guard_dur = self._get_guard_duration(tick_prog=display_prog)
                            if run_min >= guard_dur * 0.95:
                                self.log(f"Self-heal: Running with 0W for {run_min:.0f}min - forcing Unemptied", level="INFO")
                                run_minutes, duration_source = self._correct_duration(run_min)
                                self._transition_to_unemptied(skip_announce=False, run_minutes=run_minutes, energy_used=energy_used)
                                confirmed, is_human = self._get_confirmed_from_selector(classified)
                                self._save_cycle_feedback(
                                    predicted=classified,
                                    confirmed=confirmed,
                                    duration_min=run_minutes,
                                    energy_kwh=energy_used,
                                    max_power_w=self.max_power_w or 0,
                                    programme_confirmed_by_human=is_human,
                                    duration_source=duration_source,
                                    end_reason="low_power_detected",
                                )
            except Exception as e:
                self.log(f"Self-heal check failed: {e}", level="DEBUG")

        # Self-heal: Paused with door closed and low power (common after Miele door-pause during drying)
        if self.state == "Paused" and self.start_time and current_power_state not in ["unknown", "unavailable"]:
            try:
                watts = float(current_power_state or 0)
                door = self.get_state(self.door_sensor)
                if door == "off" and watts <= self.stop_w:
                    self.log("Self-heal: Paused, door closed, low power - scheduling pause-finish check", level="INFO")
                    self._schedule_pause_finish_check()
            except Exception as e:
                self.log(f"Paused self-heal check failed: {e}", level="DEBUG")

        self.log(f"DishwasherMonitor (Miele G5050 VI Active) initialized - state: {self.state}", level="INFO")

    def _now_utc(self):
        return datetime.fromtimestamp(time.time(), timezone.utc)

    def _restore_cycle_tracking_from_entity(self):
        """Restore start_time / energy_start from persisted sensor attributes (Running or Paused)."""
        try:
            attrs = (self.get_state(self.state_entity, attribute="all") or {}).get("attributes") or {}
            start_str = attrs.get("cycle_start_time")
            if start_str:
                self.start_time = _parse_utc(start_str)
            energy_at_start = attrs.get("energy_at_start")
            if self.start_time and energy_at_start is not None:
                self.energy_start = float(energy_at_start)
            elif self.start_time:
                try:
                    cur = self.get_state(self.energy_sensor)
                    used = attrs.get("energy_used")
                    if cur is not None and used is not None:
                        self.energy_start = float(cur) - float(used)
                except (TypeError, ValueError):
                    pass
            self.pause_from_low_power = bool(attrs.get("pause_from_low_power"))
        except Exception as e:
            self.log(f"Could not restore cycle tracking: {e}", level="DEBUG")

    def _local_tz(self):
        return getattr(self, "_local_tz_obj", None) or timezone.utc

    def _format_local(self, dt):
        if dt is None:
            return ""
        tz = self._local_tz()
        return dt.astimezone(tz).isoformat(timespec="seconds")

    def _format_utc(self, dt):
        if dt is None:
            return ""
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    # Fallback when classifier returns "unknown" or programme not in config (not a selectable programme)
    _UNKNOWN_FALLBACK = {"label": "—", "duration_min": 180, "max_energy_kwh": 2.0}

    def _get_profile(self, programme: str):
        """Return profile dict for programme. Uses fallback when programme unknown or missing."""
        return DishwasherMonitor.PROGRAMME_PROFILES.get(programme, DishwasherMonitor._UNKNOWN_FALLBACK)

    def _load_programme_profiles(self):
        """Load programme profiles from dishwasher_programmes.yaml."""
        prog_file = self.args.get("programmes_file") or os.path.join(
            os.path.dirname(__file__), "dishwasher_programmes.yaml"
        )
        defaults = {
            "quick": {"label": "QuickPowerWash", "duration_min": 58, "duration_short_min": 14, "max_energy_kwh": 1.55},
            "gentle": {"label": "Gentle", "duration_min": 149, "max_energy_kwh": 1.2},
            "eco": {"label": "ECO", "duration_min": 227, "duration_short_min": 74, "max_energy_kwh": 0.94},
            "auto": {"label": "Auto", "duration_min": 160, "max_energy_kwh": 1.45},
            "intensive": {"label": "Intensive", "duration_min": 150, "max_energy_kwh": 1.2},
        }
        try:
            with open(prog_file, "r") as f:
                data = yaml.safe_load(f) or {}
            profiles = data.get("programmes", {})
            if profiles:
                merged = {**defaults}
                for key, val in profiles.items():
                    if isinstance(val, dict):
                        merged[key] = {**merged.get(key, {}), **val}
                DishwasherMonitor.PROGRAMME_PROFILES = merged
                self.log(f"Loaded {len(profiles)} programme profiles from {prog_file}", level="INFO")
            else:
                DishwasherMonitor.PROGRAMME_PROFILES = defaults
        except FileNotFoundError:
            DishwasherMonitor.PROGRAMME_PROFILES = defaults
            self.log(f"Programme file {prog_file} not found - using defaults", level="WARNING")
        except Exception as exc:
            DishwasherMonitor.PROGRAMME_PROFILES = defaults
            self.log(f"Failed to load {prog_file}: {exc} - using defaults", level="ERROR")

    def _load_and_apply_feedback(self):
        """Load dishwasher_feedback.json and apply learned programme data. Only confirmed cycles."""
        self._learned_durations = {}
        if not os.path.exists(self.feedback_file):
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = __import__("json").load(f)
        except Exception as e:
            self.log(f"Could not load feedback {self.feedback_file}: {e}", level="WARNING")
            return
        cycles = data.get("cycles") or []
        for c in cycles:
            if not c.get("programme_confirmed_by_human", c.get("programme_user_confirmed", False)):
                continue
            prog = c.get("confirmed", "")
            if not prog or prog == "unknown":
                continue
            dur = c.get("duration_min")
            if dur is None or dur <= 0:
                continue
            # ECO with short=Yes learns as eco_short; QuickPowerWash with short=Yes learns as quick_short
            if prog == "eco" and c.get("short"):
                key = "eco_short"
            elif prog == "quick" and c.get("short"):
                key = "quick_short"
            else:
                key = prog
            prev = self._learned_durations.get(key, {"n": 0, "avg": float(dur)})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + float(dur)) / n_new
            self._learned_durations[key] = {"n": n_new, "avg": avg_new}
        if self._learned_durations:
            self.log(f"Loaded learned durations for {list(self._learned_durations.keys())}", level="INFO")

    def _in_eco_drying_phase(self, run_min: float) -> bool:
        """True if we're likely in ECO's long 0–3 W drying phase (run > 100 min, power low or long time since last high)."""
        if run_min <= 100:
            return False  # ECO short is 74 min; past 100 min we're in a long programme
        current_w = self._get_current_power()
        if current_w > 20:
            return False  # Still in wash/heating
        if self._last_high_power_time:
            mins_since_high = (self._now_utc() - self._last_high_power_time).total_seconds() / 60
            if mins_since_high >= 45:
                return True  # Long low-power stretch after heating = drying phase
        return run_min > 120 and current_w <= 20  # Past 2 h with low power

    def _classify_programme(self):
        """Classify programme from energy + runtime. ECO has short toggle (set in self.detected_short).
        Uses drying-phase pattern (long 0–3 W after heating) to prefer ECO full over ECO short when appropriate."""
        if not self.start_time:
            self.detected_short = False
            self.detected_quick_short = False
            return "unknown"
        run_min = (self._now_utc() - self.start_time).total_seconds() / 60
        energy = self._get_energy_used()
        if run_min < 10:
            self.detected_short = False
            self.detected_quick_short = False
            return "unknown"
        in_drying = self._in_eco_drying_phase(run_min)
        # QuickPowerWash with short = rinse/salt cycle (< 0.2 kWh, < 20 min)
        if energy < 0.2 and run_min < 20:
            self.detected_short = False
            self.detected_quick_short = True
            return "quick"
        # ECO: 0.4–0.9 kWh; short = Yes when runtime suggests short run (~74 min)
        # If we're in the long drying phase (past 100 min, low power), treat as ECO full.
        if 0.4 <= energy <= 0.9:
            self.detected_quick_short = False
            if run_min < 120 and not in_drying:
                self.detected_short = True
                return "eco"
            self.detected_short = False
            return "eco"
        # Gentle: 0.9–1.2 kWh (manual)
        if 0.9 < energy <= 1.2:
            self.detected_short = False
            self.detected_quick_short = False
            return "gentle"
        # Quick: > 1.4 kWh (full QuickPowerWash)
        if energy > 1.4:
            self.detected_short = False
            self.detected_quick_short = False
            return "quick"
        # Ambiguous: use runtime; drying phase => ECO full
        self.detected_quick_short = False
        if run_min < 100 and not in_drying:
            self.detected_short = True
            return "eco"
        if run_min < 200:
            self.detected_short = False
            return "eco"
        self.detected_short = False
        return "gentle"

    def _eco_short_active(self) -> bool:
        """True when ECO should use short duration: user set short=Yes, or classifier set detected_short."""
        if self.short_entity:
            state = self.get_state(self.short_entity)
            if state == "Yes":
                return True
            if state == "No":
                return False
        return bool(self.detected_short)

    def _quick_short_active(self) -> bool:
        """True when QuickPowerWash should use short duration (rinse/salt): user set short=Yes, or classifier set detected_quick_short."""
        if self.short_entity:
            state = self.get_state(self.short_entity)
            if state == "Yes":
                return True
            if state == "No":
                return False
        return bool(self.detected_quick_short)

    def _get_programme_duration(self, prog: str, use_learned: bool = True) -> int:
        """Expected duration in minutes. ECO/quick use duration_short_min when short=Yes (or detected)."""
        profile = self._get_profile(prog)
        if prog == "eco":
            use_short = self._eco_short_active()
            manual = profile.get("duration_short_min", 74) if use_short else profile.get("duration_min", 265)
            learn_key = "eco_short" if use_short else "eco"
        elif prog == "quick":
            use_short = self._quick_short_active()
            manual = profile.get("duration_short_min", 14) if use_short else profile.get("duration_min", 58)
            learn_key = "quick_short" if use_short else "quick"
        else:
            manual = profile.get("duration_min", 180)
            learn_key = prog
        if not use_learned:
            return int(manual)
        learned = self._learned_durations.get(learn_key)
        if learned is None or learned["n"] < 1:
            return int(manual)
        n, avg = learned["n"], learned["avg"]
        if n == 1:
            return round(0.30 * avg + 0.70 * manual)
        if n == 2:
            return round(0.50 * avg + 0.50 * manual)
        alpha = min(0.9, 0.6 + (n - 3) * (0.30 / 7))
        return round(alpha * avg + (1 - alpha) * manual)

    def _programme_max_duration_minutes(self, classification=None):
        prog = classification or self._classify_programme()
        profile = self._get_profile(prog)
        return profile.get("duration_min", int(self.max_running_hours * 60))

    def _get_guard_duration(self, tick_prog=None):
        """Duration for finish guard: require run >= this before accepting 0 W as cycle finished.
        For ECO: use full duration (e.g. 227 min) unless user explicitly set short=Yes - never use classifier's
        detected_short for the guard, or we'd allow 'finished' when power drops to 0 during drying (~1h40)."""
        if tick_prog and tick_prog != "unknown":
            profile = self._get_profile(tick_prog)
            if tick_prog == "eco":
                # Only use short duration for guard if user explicitly chose short=Yes
                if self.short_entity and self.get_state(self.short_entity) == "Yes":
                    d = profile.get("duration_short_min", 74)
                else:
                    d = profile.get("duration_min", 227)
            elif tick_prog == "quick":
                if self.short_entity and self.get_state(self.short_entity) == "Yes":
                    d = profile.get("duration_short_min", 14)
                else:
                    d = profile.get("duration_min", 58)
            else:
                d = profile.get("duration_min", 180)
            if d:
                return int(d)
        if self.expected_dur_at_start is not None:
            return self.expected_dur_at_start
        return self._programme_max_duration_minutes(classification=tick_prog)

    def _flatten_history(self, hist, entity_id=None):
        """AppDaemon get_history returns list[list[dict]]. Normalize to list[dict]."""
        if isinstance(hist, dict):
            hist = hist.get(entity_id or "", []) or hist.get("history", []) if entity_id else next(iter(hist.values()), [])
        if isinstance(hist, list) and hist and isinstance(hist[0], list):
            return hist[0]
        return hist if isinstance(hist, list) else []

    def _get_programme_duration_hint_for_history(self) -> float | None:
        """Duration hint for history correction: detected programme."""
        if self.detected_programme and self.detected_programme != "unknown":
            return float(self._get_programme_duration(self.detected_programme))
        return None

    def _estimate_cycle_end_from_history(self, expected_duration_min: float | None = None):
        """Estimate when cycle ended from HA energy history."""
        if not self.start_time or not self.energy_sensor:
            return None
        try:
            end_time = self._now_utc()
            hist = self.get_history(entity_id=self.energy_sensor, start_time=self.start_time, end_time=end_time)
            hist = self._flatten_history(hist, self.energy_sensor)
            if len(hist) < 2:
                return None
            points = []
            for entry in hist:
                try:
                    ts = entry.get("last_changed") or entry.get("last_updated")
                    if not ts:
                        continue
                    t = _parse_utc(ts)
                    if t is None:
                        continue
                    s = entry.get("state")
                    if s is None or s in ("unknown", "unavailable", ""):
                        continue
                    points.append((t, float(s)))
                except (ValueError, TypeError):
                    continue
            if len(points) < 2:
                return None
            points.sort(key=lambda x: x[0])
            run_minutes_max = (end_time - self.start_time).total_seconds() / 60
            high_end_candidates = []
            for i in range(1, len(points)):
                t1, e1 = points[i - 1]
                t2, e2 = points[i]
                delta_s = (t2 - t1).total_seconds()
                if delta_s < 10:
                    continue
                implied_w = ((e2 - e1) * 1000) / (delta_s / 3600)
                if implied_w > self.energy_active_watts:
                    high_end_candidates.append(t2)
            if not high_end_candidates:
                return None
            if expected_duration_min and expected_duration_min > 0:
                best_end, best_diff = None, float("inf")
                for t in high_end_candidates:
                    dur = (t - self.start_time).total_seconds() / 60
                    if dur < self.min_cycle_minutes or dur > run_minutes_max:
                        continue
                    diff = abs(dur - expected_duration_min)
                    if diff < best_diff:
                        best_diff, best_end = diff, t
                if best_end:
                    return best_end + timedelta(minutes=2)
            last = high_end_candidates[-1]
            return last + timedelta(minutes=2)
        except Exception as e:
            self.log(f"Could not estimate cycle end from history: {e}", level="DEBUG")
            return None

    def _correct_duration(self, run_minutes_wall: float, log_prefix: str = "") -> tuple:
        """Correct wall-clock duration using HA history. Returns (run_minutes, duration_source).
        Only applies correction when history gives a duration close to wall-clock (within 15%),
        to avoid wrongly shortening e.g. ECO ~4h to ~1.5h when energy series has sparse/coarse data."""
        run_minutes = run_minutes_wall
        duration_source = None
        hint = self._get_programme_duration_hint_for_history()
        actual_end = self._estimate_cycle_end_from_history(expected_duration_min=hint)
        if actual_end and self.start_time:
            run_minutes_actual = (actual_end - self.start_time).total_seconds() / 60
            # Require corrected duration to be at least 85% of wall-clock (avoid false shortening)
            min_acceptable = run_minutes_wall * 0.85
            if (
                run_minutes_actual >= self.min_cycle_minutes
                and run_minutes_actual <= run_minutes
                and run_minutes_actual >= min_acceptable
            ):
                delta = run_minutes - run_minutes_actual
                if delta > 1.0:
                    self.log(f"{log_prefix}Using HA history: {run_minutes_actual:.1f} min (detection was {delta:.0f} min late)", level="INFO")
                run_minutes = run_minutes_actual
                duration_source = "history_corrected"
            elif run_minutes_actual < min_acceptable and run_minutes_actual >= self.min_cycle_minutes:
                self.log(
                    f"{log_prefix}History correction ignored: {run_minutes_actual:.0f} min << wall {run_minutes_wall:.0f} min (would wrongly shorten run)",
                    level="DEBUG",
                )
        return (run_minutes, duration_source)

    # UI label -> programme key. ECO is one programme; short is separate (input_select.dishwasher_short Yes/No).
    _SELECTOR_LABEL_TO_KEY = {
        "QuickPowerWash": "quick", "Gentle": "gentle", "ECO": "eco", "Auto": "auto",
        "Intensive": "intensive",
    }

    def _get_confirmed_from_selector(self, predicted: str) -> tuple:
        """Return (confirmed_programme_key, programme_confirmed_by_human) from selector if configured."""
        if not self.confirmed_programme_entity:
            return (predicted, False)
        state = self.get_state(self.confirmed_programme_entity)
        # "—" / "Not set" = unconfirmed; "Auto" = actual Auto programme (not the same)
        if state is None or state in ("", "—", "Not set", "Auto (unconfirmed)", "unknown"):
            return (predicted, False)
        key = self._SELECTOR_LABEL_TO_KEY.get(state)
        if key is None:
            return (predicted, False)
        return (key, True)

    def _save_cycle_feedback(
        self,
        predicted: str,
        confirmed: str,
        duration_min: float,
        energy_kwh: float,
        max_power_w: float,
        programme_confirmed_by_human: bool = False,
        duration_source: str | None = None,
        end_reason: str | None = None,
        idle_min: float | None = None,
    ):
        """Append one cycle record to dishwasher_feedback.json. Every completed cycle is saved;
        programme_confirmed_by_human is True only when user set the programme selector (e.g. ECO), else False (unconfirmed)."""
        import json
        record = {
            "ts": self._format_local(self._now_utc()),
            "duration_min": round(duration_min, 1),
            "energy_kwh": round(energy_kwh, 3),
            "predicted": predicted,
            "confirmed": confirmed,
            "programme_confirmed_by_human": programme_confirmed_by_human,
            "max_power_w": round(max_power_w, 0),
        }
        if confirmed == "eco":
            record["short"] = self._eco_short_active()
        if confirmed == "quick":
            record["short"] = self._quick_short_active()
        if duration_source:
            record["duration_source"] = duration_source
        if end_reason:
            record["end_reason"] = end_reason
        if idle_min is not None and idle_min >= 0:
            record["idle_min"] = round(idle_min, 1)
        data = {"version": 2, "cycles": []}
        if os.path.exists(self.feedback_file):
            try:
                with open(self.feedback_file, "r") as f:
                    data = json.load(f)
            except Exception:
                pass
        data["version"] = 2
        data.setdefault("cycles", []).append(record)
        path = os.path.abspath(self.feedback_file)
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write feedback to {path}: {e}", level="WARNING")
            return
        if programme_confirmed_by_human:
            prev = self._learned_durations.get(confirmed, {"n": 0, "avg": duration_min})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + duration_min) / n_new
            self._learned_durations[confirmed] = {"n": n_new, "avg": avg_new}
        status = "confirmed" if programme_confirmed_by_human else "unconfirmed"
        self.log(f"Feedback saved: {confirmed} ({status}) duration {duration_min:.0f}min energy {energy_kwh:.2f}kWh", level="INFO")

    def _remove_last_cycle_feedback(self):
        """Remove last cycle from feedback (recovery from false Unemptied)."""
        import json
        if not os.path.exists(self.feedback_file):
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = json.load(f)
        except Exception:
            return
        cycles = data.get("cycles") or []
        if not cycles:
            return
        removed = cycles.pop()
        confirmed = removed.get("confirmed", "")
        duration_min = removed.get("duration_min", 0)
        if confirmed and duration_min and confirmed in self._learned_durations:
            old = self._learned_durations[confirmed]
            n = old["n"] - 1
            if n <= 0:
                del self._learned_durations[confirmed]
            else:
                avg_new = (old["avg"] * old["n"] - duration_min) / n
                self._learned_durations[confirmed] = {"n": n, "avg": avg_new}
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write feedback after recovery: {e}", level="WARNING")
        self.log(f"Removed false cycle from feedback ({duration_min:.0f}min, {confirmed})", level="INFO")

    def _mark_last_cycle_emptied(self):
        """Append emptied_ts to the last cycle in feedback so we have a record that the user emptied."""
        import json
        if not os.path.exists(self.feedback_file):
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Could not read feedback for emptied_ts: {e}", level="DEBUG")
            return
        cycles = data.get("cycles") or []
        if not cycles:
            return
        cycles[-1]["emptied_ts"] = self._format_local(self._now_utc())
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write emptied_ts to feedback: {e}", level="DEBUG")

    def _clear_start_candidate_timers(self):
        self._safe_cancel_timer(self._start_candidate_window_timer)
        self._start_candidate_window_timer = None
        self._safe_cancel_timer(self._start_candidate_idle_timer)
        self._start_candidate_idle_timer = None

    def _cancel_start_candidate(self, reason, level="INFO"):
        """Abandon start candidate without publishing Running. Does not touch last_state_change (cooling)."""
        if not self._start_candidate_active:
            self._clear_start_candidate_timers()
            return
        self.log(
            f"Start candidate cancelled ({self._start_candidate_source}): {reason}",
            level=level,
        )
        self._start_candidate_active = False
        self._start_candidate_source = None
        self._start_candidate_active_samples = 0
        self._start_candidate_max_w = 0.0
        self._start_candidate_began_at = None
        self._start_candidate_energy_at_start = None
        self._clear_start_candidate_timers()
        self.high_power_counter = 0

    def _schedule_start_candidate_window(self):
        self._safe_cancel_timer(self._start_candidate_window_timer)
        self._start_candidate_window_timer = self.run_in(
            self._start_candidate_window_expired,
            self.start_candidate_window_s,
        )

    def _start_candidate_window_expired(self, _kwargs):
        self._start_candidate_window_timer = None
        if not self._start_candidate_active:
            return
        self._cancel_start_candidate(
            f"validation window {self.start_candidate_window_s}s expired without commit",
            level="INFO",
        )

    def _start_candidate_idle_expired(self, _kwargs):
        self._start_candidate_idle_timer = None
        if not self._start_candidate_active:
            return
        try:
            ps = self.get_state(self.power_sensor)
            if ps in ("unknown", "unavailable", None):
                return
            w = float(ps or 0)
        except (TypeError, ValueError):
            return
        if w > self.start_candidate_idle_w:
            return
        self._cancel_start_candidate(
            f"idle collapse (power stayed <= {self.start_candidate_idle_w}W for {self.start_candidate_idle_grace_s}s)",
            level="INFO",
        )

    def _maybe_schedule_start_candidate_idle(self, watts: float):
        if not self._start_candidate_active or watts > self.start_candidate_idle_w:
            self._safe_cancel_timer(self._start_candidate_idle_timer)
            self._start_candidate_idle_timer = None
            return
        if self._start_candidate_idle_timer and self.timer_running(self._start_candidate_idle_timer):
            return
        self._start_candidate_idle_timer = self.run_in(
            self._start_candidate_idle_expired,
            self.start_candidate_idle_grace_s,
        )

    def _start_candidate_energy_delta_kwh(self):
        """Energy (kWh) gained since this candidate began; None if not measurable."""
        if self._start_candidate_energy_at_start is None:
            return None
        cur = self._read_energy_kwh_optional()
        if cur is None:
            return None
        return cur - self._start_candidate_energy_at_start

    def _start_candidate_commit_ready(self):
        if self._start_candidate_active_samples >= self.start_candidate_extended_active_samples:
            return True
        if (
            self._start_candidate_active_samples >= self.start_candidate_min_active_samples
            and self._start_candidate_max_w >= self.start_candidate_min_peak_w
        ):
            return True
        ed = self._start_candidate_energy_delta_kwh()
        if (
            self.start_candidate_min_energy_delta_kwh > 0
            and ed is not None
            and ed >= self.start_candidate_min_energy_delta_kwh
            and self._start_candidate_active_samples >= self.start_candidate_min_active_samples
        ):
            return True
        return False

    def _publish_running_after_start_validation(
        self,
        watts_confirm: float,
        *,
        from_unemptied_recovery: bool = False,
        commit_reason: str = "",
    ):
        """Publish Running after start candidate or strict sustain; mirrors former _confirm_running body."""
        force = bool(from_unemptied_recovery)
        if not self._should_change_state("Running", force=force):
            self.pending_start_used_fast_path = False
            self._cancel_start_candidate("Running publish blocked (cooling or duplicate)", level="DEBUG")
            return

        self._clear_start_candidate_timers()
        self._start_candidate_active = False
        self._start_candidate_source = None
        self._start_candidate_active_samples = 0
        self._start_candidate_max_w = 0.0
        self._start_candidate_began_at = None
        self._start_candidate_energy_at_start = None

        if from_unemptied_recovery:
            self.log(
                f"Recovering from false Unemptied (validated): power {watts_confirm:.1f}W -> Running",
                level="WARNING",
            )
            self._remove_last_cycle_feedback()

        self.state = "Running"
        self._strict_start_until_door_or_sustain = False
        self._sustain_start_begin = None
        self.start_time = self._now_utc()
        if (
            self.pending_start_used_fast_path
            and self.last_door_closed_at
            and self.start_time < self.last_door_closed_at
        ):
            self.start_time = self.last_door_closed_at
        self.notification_sent = False
        self.door_opened_during_cycle = False
        self.detected_programme = "unknown"
        self.detected_short = False
        self.detected_quick_short = False
        self.max_power_w = watts_confirm

        try:
            energy = self.get_state(self.energy_sensor)
            if energy is not None:
                self.energy_start = float(energy)
        except (ValueError, TypeError):
            pass

        classified = self._classify_programme()
        display_prog = self._get_programme_for_display()
        self.detected_programme = display_prog
        self.expected_dur_at_start = self._get_guard_duration(tick_prog=display_prog)
        self._set_state_entity(state="Running")
        self._update_running_attributes()

        if self.reset_programme_selector_on_start and self.confirmed_programme_entity:
            try:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.confirmed_programme_entity,
                    option="—",
                )
            except Exception as e:
                self.log(f"Could not reset programme selector: {e}", level="DEBUG")

        if not self.poll_timer:
            self.poll_timer = self.run_in(self._poll_power, 60)
        if not self.classify_timer:
            self.classify_timer = self.run_in(self._tick_classify, 10)

        self._safe_cancel_timer(self.running_watchdog_timer)
        self.running_watchdog_timer = self.run_in(
            self._running_watchdog_timeout,
            int(self.max_running_hours * 3600),
        )

        reason_txt = f" ({commit_reason})" if commit_reason else ""
        self.log(f"State -> Running{reason_txt}", level="INFO")
        self.pending_start_used_fast_path = False
        self.door_fast_start_armed_until = None

        if from_unemptied_recovery:
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None

    def _feed_off_to_running_start_candidate(self, watts: float, *, force_commit: bool = False):
        """Accumulate evidence for Off->Running; publish only when confident (or force_commit after strict sustain)."""
        if self.get_state(self.state_entity) != "Off":
            return
        if force_commit:
            if watts >= self.start_w:
                self.log(
                    f"Start candidate force-commit (strict sustain): {watts:.1f}W >= {self.start_w}W",
                    level="INFO",
                )
                self._publish_running_after_start_validation(
                    watts,
                    from_unemptied_recovery=False,
                    commit_reason="strict_sustain",
                )
            else:
                self.log(
                    f"Start candidate force-commit skipped: power {watts:.1f}W < {self.start_w}W after sustain gate",
                    level="DEBUG",
                )
            return

        now = self._now_utc()
        if not self._start_candidate_active or self._start_candidate_source != "off":
            self._start_candidate_active = True
            self._start_candidate_source = "off"
            self._start_candidate_active_samples = 0
            self._start_candidate_max_w = 0.0
            self._start_candidate_began_at = now
            self._schedule_start_candidate_window()
            self._start_candidate_energy_at_start = self._read_energy_kwh_optional()
            self.log(
                f"Start candidate (Off): began validation window={self.start_candidate_window_s}s",
                level="DEBUG",
            )

        if watts >= self.start_candidate_high_confidence_w:
            self.log(
                f"Start candidate (Off): high-confidence {watts:.1f}W >= {self.start_candidate_high_confidence_w}W -> Running",
                level="INFO",
            )
            self._publish_running_after_start_validation(
                watts, from_unemptied_recovery=False, commit_reason="high_power_w"
            )
            return

        self._start_candidate_max_w = max(self._start_candidate_max_w, float(watts))
        if watts >= self.start_w:
            self._start_candidate_active_samples += 1
            self._safe_cancel_timer(self._start_candidate_idle_timer)
            self._start_candidate_idle_timer = None

        if self._start_candidate_commit_ready():
            if self._start_candidate_active_samples >= self.start_candidate_extended_active_samples:
                cr = "active_samples_extended_window"
            elif (
                self._start_candidate_active_samples >= self.start_candidate_min_active_samples
                and self._start_candidate_max_w >= self.start_candidate_min_peak_w
            ):
                cr = "active_samples_and_peak_w"
            else:
                cr = "energy_delta_tiebreak"
            self.log(
                f"Start candidate (Off): commit samples={self._start_candidate_active_samples} "
                f"max_w={self._start_candidate_max_w:.1f}W ({cr})",
                level="INFO",
            )
            self._publish_running_after_start_validation(
                watts, from_unemptied_recovery=False, commit_reason=cr
            )
            return

        self._maybe_schedule_start_candidate_idle(watts)

    def _feed_unemptied_recovery_start_candidate(self, watts: float):
        """Validate Unemptied->Running recovery; avoids single-sample false recovery."""
        if self.get_state(self.state_entity) != "Unemptied":
            return

        if watts >= self.start_candidate_high_confidence_w:
            self.log(
                f"Start candidate (Unemptied recovery): high-confidence {watts:.1f}W -> Running",
                level="INFO",
            )
            self._publish_running_after_start_validation(
                watts, from_unemptied_recovery=True, commit_reason="unemptied_recovery_high_power_w"
            )
            return

        now = self._now_utc()
        if not self._start_candidate_active or self._start_candidate_source != "unemptied_recovery":
            self._start_candidate_active = True
            self._start_candidate_source = "unemptied_recovery"
            self._start_candidate_active_samples = 0
            self._start_candidate_max_w = 0.0
            self._start_candidate_began_at = now
            self._schedule_start_candidate_window()
            self._start_candidate_energy_at_start = self._read_energy_kwh_optional()
            self.log(
                f"Start candidate (Unemptied recovery): began validation window={self.start_candidate_window_s}s",
                level="DEBUG",
            )

        self._start_candidate_max_w = max(self._start_candidate_max_w, float(watts))
        if watts >= self.start_w:
            self._start_candidate_active_samples += 1
            self._safe_cancel_timer(self._start_candidate_idle_timer)
            self._start_candidate_idle_timer = None

        if self._start_candidate_commit_ready():
            if self._start_candidate_active_samples >= self.start_candidate_extended_active_samples:
                cr = "unemptied_recovery_extended_samples"
            elif (
                self._start_candidate_active_samples >= self.start_candidate_min_active_samples
                and self._start_candidate_max_w >= self.start_candidate_min_peak_w
            ):
                cr = "unemptied_recovery_samples_and_peak_w"
            else:
                cr = "unemptied_recovery_energy_delta_tiebreak"
            self.log(
                f"Start candidate (Unemptied recovery): commit samples={self._start_candidate_active_samples} "
                f"max_w={self._start_candidate_max_w:.1f}W -> Running ({cr})",
                level="INFO",
            )
            self._publish_running_after_start_validation(
                watts, from_unemptied_recovery=True, commit_reason=cr
            )
            return

        self._maybe_schedule_start_candidate_idle(watts)

    def _recover_from_false_unemptied_commit(self, watts: float):
        """Immediate Unemptied->Running recovery (legacy path when candidate disabled)."""
        self.log(f"Recovering from false Unemptied: power {watts:.1f}W - reverting to Running", level="WARNING")
        self._remove_last_cycle_feedback()
        self.state = "Running"
        self._set_state_entity(state="Running")
        self._safe_cancel_timer(self.unemptied_watchdog_timer)
        self.unemptied_watchdog_timer = None
        self._safe_cancel_timer(self.running_watchdog_timer)
        self.running_watchdog_timer = self.run_in(
            self._running_watchdog_timeout,
            int(self.max_running_hours * 3600),
        )
        if not self.poll_timer:
            self.poll_timer = self.run_in(self._poll_power, 60)

    def _recover_from_false_unemptied(self, watts: float):
        """Power went high again after Unemptied - machine still running (validated when candidate enabled)."""
        self._feed_unemptied_recovery_start_candidate(watts)

    def _get_programme_for_display(self):
        """Programme to use for display and duration/ETA: user-selected (selector) if set, else classified."""
        prog = self._classify_programme()
        if self.confirmed_programme_entity:
            state = self.get_state(self.confirmed_programme_entity)
            if state and state not in ("", "—", "Not set", "Auto (unconfirmed)", "unknown"):
                key = self._SELECTOR_LABEL_TO_KEY.get(state)
                if key and key != "unknown":
                    return key  # User confirmed programme -> use everywhere, don't show classifier guess
        return prog

    def _update_running_attributes(self):
        """Update state entity with progress/ETA attributes while Running."""
        if self.get_state(self.state_entity) != "Running" or not self.start_time:
            return
        classified = self._classify_programme()
        # When user has confirmed programme (selector set), use it for display AND duration; else use classified
        display_prog = self._get_programme_for_display()
        self.detected_programme = display_prog  # Show confirmed programme when set, not classifier guess
        profile = self._get_profile(display_prog)
        label = profile.get("label", display_prog)
        dur = self._get_programme_duration(display_prog)
        elapsed = (self._now_utc() - self.start_time).total_seconds() / 60
        remaining = max(0, dur - elapsed)
        progress = min(100, round(100 * elapsed / dur)) if dur > 0 else 0
        eta = self.start_time + timedelta(minutes=dur) if dur else None
        attrs = {
            "detected_programme": display_prog,
            "programme_label": label,
            "classified_programme": classified if classified != display_prog else None,  # for debugging when user overrides
            "cycle_start_time": self._format_utc(self.start_time),
            "short_selected": self.get_state(self.short_entity) if self.short_entity else None,
            "short_detected": self.detected_short if display_prog == "eco" else None,
            "started_at_display": self._format_local(self.start_time),
            "programme_duration_min": dur,
            "elapsed_minutes": round(elapsed, 1),
            "progress_pct": progress,
            "estimated_remaining_min": round(remaining),
            "estimated_end_time": self._format_utc(eta) if eta else None,
            "energy_at_start": self.energy_start,
            "energy_used": round(self._get_energy_used(), 3),
        }
        try:
            full = self.get_state(self.state_entity, attribute="all")
            existing = dict((full or {}).get("attributes") or {})
            existing.update(attrs)
            # elapsed_minutes/progress_pct/energy_used silently drop from published attributes at
            # 0/0.0 (every cycle's first tick(s)); estimated_remaining_min drops the same way on
            # overrun, and short_detected drops while still False (early ECO, before short confirms)
            # -- AppDaemon 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity( state="Running", attributes=existing, replace=True)
        except Exception:
            pass

    def _get_current_power(self):
        """Get current power reading in watts."""
        try:
            power_state = self.get_state(self.power_sensor)
            if power_state not in ["unknown", "unavailable", None]:
                return float(power_state)
        except (ValueError, TypeError):
            pass
        return 0

    def _get_run_duration_minutes(self):
        """Get how long the current cycle has been running in minutes."""
        if self.start_time is None:
            return 0
        return (self._now_utc() - self.start_time).total_seconds() / 60

    def _read_energy_kwh_optional(self):
        """Current cumulative energy sensor (kWh), or None if missing/unavailable."""
        try:
            e = self.get_state(self.energy_sensor)
            if e is None or e in ("unknown", "unavailable"):
                return None
            return float(e)
        except (TypeError, ValueError):
            return None

    def _get_energy_used(self):
        """Get energy consumed since cycle start in kWh."""
        if self.energy_start is None:
            return 0
        try:
            current_energy = self.get_state(self.energy_sensor)
            if current_energy is not None:
                return float(current_energy) - self.energy_start
        except (ValueError, TypeError):
            pass
        return 0

    def _is_valid_completed_cycle(self):
        """Check if the cycle ran long enough and used enough energy to be considered complete."""
        run_minutes = self._get_run_duration_minutes()
        energy_used = self._get_energy_used()

        if energy_used < 0:
            self.log(
                f"Cycle validation: energy_used={energy_used:.3f} kWh (negative - counter reset or sensor issue?), "
                f"run_minutes={run_minutes:.1f}; treating as invalid",
                level="WARNING",
            )
            return False

        is_valid = (run_minutes >= self.min_cycle_minutes and
                    energy_used >= self.min_energy_kwh)

        self.log(f"Cycle validation: {run_minutes:.1f} min (need {self.min_cycle_minutes}), "
                 f"{energy_used:.3f} kWh (need {self.min_energy_kwh}) -> {'valid' if is_valid else 'invalid'}",
                 level="INFO")

        return is_valid

    def _should_change_state(self, new_state, force=False):
        """Check if we should allow a state change."""
        if self.get_state(self.state_entity) == new_state:
            return False
        if not force:
            now = datetime.now()
            if self.last_state_change and (now - self.last_state_change).total_seconds() < self.cooling_period:
                self.log(f"In cooling period", level="DEBUG")
                return False
        self.last_state_change = datetime.now()
        return True

    def _record_power_reading(self, watts):
        """Record power readings to detect patterns"""
        self.power_readings.append(watts)
        if len(self.power_readings) > self.pattern_window:
            self.power_readings.pop(0)

    def _door_state_changed(self, entity, attr, old, new, kwargs):
        """Handle door open and close events."""
        current_state = self.get_state(self.state_entity)

        if new == "on":  # Door opened
            self._handle_door_opened(current_state)
        elif new == "off":  # Door closed
            self._handle_door_closed(current_state)

    def _handle_door_opened(self, current_state):
        """Handle door opening. Fill window: before 74 min, door+low power = Off; door+high power = Paused.
        When door opens with low power: machine may have paused (power drops). Only treat as 'cycle done, emptying'
        if we're past the full programme duration; otherwise treat as Paused (door open during run)."""
        current_power = self._get_current_power()
        elapsed = self._get_run_duration_minutes()
        self.log(f"Door opened, state: {current_state}, power: {current_power:.1f}W, elapsed: {elapsed:.1f}min", level="DEBUG")

        if getattr(self, "_start_candidate_active", False):
            self._cancel_start_candidate("door_opened", level="DEBUG")

        if current_state in ("Off", "Unemptied", "Emptied", "Error"):
            self._strict_start_until_door_or_sustain = False
            self._sustain_start_begin = None

        if current_state in ("Off", "Emptied", "Error"):
            self.door_fast_start_armed_until = None
            self.high_power_counter = 0

        if current_state == "Running":
            if current_power <= self.stop_w:
                # Low power - either cycle finished (user emptying) or machine paused because door opened
                if elapsed < self.fill_window_minutes:
                    # Machine rinse (14 min) or interrupted - not a valid cycle
                    self.log(f"Door opened with low power before {self.fill_window_minutes}min - Off (rinse/interrupted)", level="INFO")
                    self._transition_to_off("Door opened before fill window - rinse or interrupted")
                else:
                    # Use same guard as power-based finish: only treat as "cycle done" if past full programme duration
                    display_prog = self._get_programme_for_display()
                    guard_dur = self._get_guard_duration(tick_prog=display_prog)
                    if elapsed < guard_dur:
                        # Still within programme time - machine paused when door opened (e.g. ECO with 2h left)
                        self.log(
                            f"Door opened with low power at {elapsed:.0f}min < {guard_dur:.0f}min (programme still running) -> Paused",
                            level="INFO",
                        )
                        self.door_opened_time = datetime.now()
                        self.door_opened_during_cycle = True
                        self._transition_to_paused(low_power=True)
                    else:
                        # Past full duration - cycle done, user emptying (door opened first)
                        self.log(f"Door opened with low power ({current_power:.1f}W) - user is emptying -> Unemptied then Emptied", level="INFO")
                        run_minutes_wall = elapsed
                        run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Door-open")
                        idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                        energy_kwh = self._get_energy_used()
                        prog = self._classify_programme()
                        self._transition_to_unemptied(skip_announce=True, run_minutes=run_minutes, energy_used=energy_kwh)
                        confirmed, is_human = self._get_confirmed_from_selector(prog)
                        self._save_cycle_feedback(
                            predicted=prog,
                            confirmed=confirmed,
                            duration_min=run_minutes,
                            energy_kwh=energy_kwh,
                            max_power_w=self.max_power_w,
                            programme_confirmed_by_human=is_human,
                            duration_source=duration_source,
                            end_reason="door_opened_first",
                            idle_min=idle_min,
                        )
                        self._transition_to_emptied("Door opened after cycle complete - emptying")
            else:
                # High power - adding dishes (Paused)
                self.door_opened_time = datetime.now()
                self.door_opened_during_cycle = True
                self._transition_to_paused(low_power=False)

        elif current_state == "Paused":
            self.door_opened_time = datetime.now()

        elif current_state == "Unemptied":
            self._transition_to_emptied("Door opened - emptying")

    def _handle_door_closed(self, current_state):
        """Handle door closing event."""
        self.log(f"Door closed, current state: {current_state}", level="DEBUG")

        if current_state == "Paused":
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None

            current_power = self._get_current_power()

            if current_power >= self.start_w:
                self._safe_cancel_timer(self.pause_finish_timer)
                self.pause_finish_timer = None
                self._transition_to_running_from_pause()
            else:
                # Power not yet back (machine may resume in a few seconds).
                elapsed = self._get_run_duration_minutes()
                display_prog = self._get_programme_for_display()
                guard_dur = self._get_guard_duration(tick_prog=display_prog)
                if elapsed >= guard_dur:
                    self._evaluate_pause_exit(force=True)
                elif self.pause_from_low_power:
                    # Miele often drops to 0W when door opens during drying; after door close, do not wait forever.
                    self.log(
                        f"Door closed while Paused (low-power pause) at {elapsed:.0f}min — "
                        f"will check for finish after {self.stop_for}s if power stays low",
                        level="INFO",
                    )
                    self._schedule_pause_finish_check()
                    self.run_in(self._check_pause_resume, 10)
                else:
                    self.log(
                        f"Door closed while Paused (adding dishes), power still low — waiting for cycle to resume",
                        level="INFO",
                    )
                    self.run_in(self._check_pause_resume, 10)

        elif current_state == "Emptied":
            # If door was open only briefly, treat as peek (user didn't know it had run) -> stay Unemptied
            if self.emptied_at is not None and self.min_emptying_seconds > 0:
                elapsed = (self._now_utc() - self.emptied_at).total_seconds()
                if elapsed < self.min_emptying_seconds:
                    self._safe_cancel_timer(self.emptied_timeout_timer)
                    self.emptied_timeout_timer = None
                    self.emptied_at = None
                    self._revert_emptied_to_unemptied()
                    return
            # Door closed after emptying - cycle complete, go to Off
            self._safe_cancel_timer(self.emptied_timeout_timer)
            self.emptied_timeout_timer = None
            self.emptied_at = None
            self.log(f"Door closed after emptying -> Off", level="INFO")
            self._strict_start_until_door_or_sustain = False
            self._sustain_start_begin = None
            self._transition_to_off("Door closed - emptying complete")
            now = self._now_utc()
            self.last_door_closed_at = now
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

        elif current_state == "Off":
            now = self._now_utc()
            self.last_door_closed_at = now
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

    def _check_pause_resume(self, kwargs):
        """Called after door closed while Paused with low power; re-check power and restore Running if machine resumed."""
        if self.get_state(self.state_entity) != "Paused":
            return
        try:
            w = float(self.get_state(self.power_sensor) or 0)
        except (ValueError, TypeError):
            return
        if w >= self.start_w:
            self._transition_to_running_from_pause()

    def _transition_to_paused(self, low_power=False):
        """Transition to Paused when door opens during Running (add dishes or Miele door-pause)."""
        self.pause_from_low_power = bool(low_power)
        if self._should_change_state("Paused", force=True):
            self.state = "Paused"
            reason = (
                "Door opened during cycle (machine paused — low power)"
                if low_power
                else "Door opened during cycle (adding dishes)"
            )
            try:
                full = self.get_state(self.state_entity, attribute="all") or {}
                pause_attrs = dict((full.get("attributes") or {}))
            except Exception:
                pause_attrs = {}
            pause_attrs["reason"] = reason
            pause_attrs["pause_from_low_power"] = self.pause_from_low_power
            pause_attrs["run_time_minutes"] = round(self._get_run_duration_minutes(), 1)
            pause_attrs["energy_used"] = round(self._get_energy_used(), 3)
            if self.start_time:
                pause_attrs["cycle_start_time"] = self._format_utc(self.start_time)
            if self.energy_start is not None:
                pause_attrs["energy_at_start"] = self.energy_start
            # pause_from_low_power silently drops from published attributes whenever it's False
            # (the common "adding dishes" pause, vs. the low-power/Miele-pause variant) -- AppDaemon
            # 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity(state="Paused", attributes=pause_attrs, replace=True)
            label = "machine paused (low power)" if low_power else "adding dishes mid-cycle"
            self.log(f"State -> Paused ({label})", level="INFO")

            # Short timeout - if door stays open too long, assume emptied
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
            self._strict_start_until_door_or_sustain = True
            self._transition_to_off(f"Watchdog: ran {run_hours:.1f}h (max {self.max_running_hours}h)")
        self.running_watchdog_timer = None

    def _unemptied_watchdog_timeout(self, kwargs):
        """Safety watchdog - Unemptied too long, user may have emptied without door sensor detecting."""
        current_state = self.get_state(self.state_entity)
        if current_state == "Unemptied":
            self.log(f"WATCHDOG: Unemptied for {self.unemptied_timeout_hours}h - assuming emptied", level="WARNING")
            self._strict_start_until_door_or_sustain = True
            self._transition_to_off(f"Watchdog: unemptied timeout ({self.unemptied_timeout_hours}h)")
        self.unemptied_watchdog_timer = None

    def _transition_to_running_from_pause(self):
        """Resume Running state after pause. Preserve start_time and confirmed programme; refresh attributes so UI shows correct remaining time."""
        self._safe_cancel_timer(self.pause_finish_timer)
        self.pause_finish_timer = None
        if self._should_change_state("Running"):
            self.state = "Running"
            self.door_opened_time = None
            self.pause_from_low_power = False
            self._set_state_entity( state="Running")
            # Refresh attributes immediately so confirmed programme and ETA (e.g. 2h left) are correct
            self._update_running_attributes()
            self.log("State -> Running (resumed after pause)", level="INFO")

            if not self.poll_timer:
                self.poll_timer = self.run_in(self._poll_power, 60)
            if not self.classify_timer:
                self.classify_timer = self.run_in(self._tick_classify, 10)

    def _schedule_pause_finish_check(self):
        """After low-power door pause with door closed, confirm finish if power stays low."""
        self._safe_cancel_timer(self.pause_finish_timer)
        self.pause_finish_timer = self.run_in(self._confirm_pause_finished, self.stop_for)

    def _confirm_pause_finished(self, kwargs):
        """Door closed while Paused with low power: if power stays low and cycle looks valid, finish."""
        self.pause_finish_timer = None
        if self.get_state(self.state_entity) != "Paused":
            return
        if self.get_state(self.door_sensor) == "on":
            return
        watts = self._get_current_power()
        if watts >= self.start_w:
            self._transition_to_running_from_pause()
            return
        if watts <= self.stop_w and self._is_valid_completed_cycle():
            self.log(
                f"Paused finish check: power still <= {self.stop_w}W after {self.stop_for}s — evaluating pause exit",
                level="INFO",
            )
            self._evaluate_pause_exit(force=True)

    def _evaluate_pause_exit(self, force=False):
        """Determine whether to go to Unemptied or Off when exiting Paused state."""
        self._safe_cancel_timer(self.pause_finish_timer)
        self.pause_finish_timer = None
        if self._is_valid_completed_cycle():
            run_minutes_wall = self._get_run_duration_minutes()
            run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Pause-exit")
            idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
            energy_kwh = self._get_energy_used()
            prog = self._classify_programme()
            self._transition_to_unemptied(skip_announce=False, run_minutes=run_minutes, energy_used=energy_kwh)
            confirmed, is_human = self._get_confirmed_from_selector(prog)
            self._save_cycle_feedback(
                predicted=prog,
                confirmed=confirmed,
                duration_min=run_minutes,
                energy_kwh=energy_kwh,
                max_power_w=self.max_power_w,
                programme_confirmed_by_human=is_human,
                duration_source=duration_source,
                end_reason="low_power_detected",
                idle_min=idle_min,
            )
        else:
            self._transition_to_off("Cycle interrupted or incomplete")

    def _off_merged_attributes(self, reason):
        """Merge Off transition into existing sensor attributes so stale Running fields disappear in HA."""
        cleared = {
            "reason": reason,
            "error": None,
            "detected_programme": "unknown",
            "programme_label": "—",
            "classified_programme": None,
            "cycle_start_time": "",
            "short_selected": None,
            "short_detected": None,
            "started_at_display": "",
            "programme_duration_min": None,
            "elapsed_minutes": None,
            "progress_pct": None,
            "estimated_remaining_min": None,
            "estimated_end_time": "",
            "energy_at_start": None,
            "energy_used": None,
            "run_time_minutes": None,
        }
        try:
            full = self.get_state(self.state_entity, attribute="all") or {}
            existing = dict((full.get("attributes") or {}))
            for k, v in cleared.items():
                if v is None:
                    existing.pop(k, None)
                else:
                    existing[k] = v
            return existing
        except Exception:
            return {k: v for k, v in cleared.items() if v is not None}

    def _unemptied_merged_attributes(self, run_minutes, energy_used):
        """Merge Unemptied transition into existing sensor attributes; strip stale Running fields."""
        cleared = {
            "reason": None,
            "detected_programme": "unknown",
            "programme_label": "—",
            "classified_programme": None,
            "cycle_start_time": "",
            "short_detected": None,
            "started_at_display": "",
            "programme_duration_min": None,
            "elapsed_minutes": None,
            "progress_pct": None,
            "estimated_remaining_min": None,
            "estimated_end_time": "",
            "energy_at_start": None,
            "run_time_minutes": round(float(run_minutes), 1),
        }
        if energy_used is not None and float(energy_used) > 0:
            cleared["energy_used"] = round(float(energy_used), 3)
        else:
            cleared["energy_used"] = None
        if self.short_entity:
            cleared["short_selected"] = self.get_state(self.short_entity)
        try:
            full = self.get_state(self.state_entity, attribute="all") or {}
            existing = dict((full.get("attributes") or {}))
            for k, v in cleared.items():
                if v is None:
                    existing.pop(k, None)
                else:
                    existing[k] = v
            return existing
        except Exception:
            return {k: v for k, v in cleared.items() if v is not None}

    def _transition_to_off(self, reason, force=False):
        """Transition to Off state."""
        if self._should_change_state("Off", force=force):
            self.state = "Off"
            self._set_state_entity(state="Off", attributes=self._off_merged_attributes(reason), replace=True)
            self.log(f"State -> Off ({reason})", level="INFO")
            self._reset_cycle_tracking()

    def _transition_to_unemptied(self, skip_announce=False, run_minutes=None, energy_used=None, force=False):
        """Transition to Unemptied state (cycle done, door still closed, waiting for user).

        skip_announce: If True, do not send the "dishwasher ready to empty" notification.
        run_minutes, energy_used: Optional corrected values (from history correction).
        force: If True, bypass cooling period (e.g. dishwasher_force_unemptied).
        """
        if self._should_change_state("Unemptied", force=force):
            run_minutes = run_minutes if run_minutes is not None else self._get_run_duration_minutes()
            energy_used = energy_used if energy_used is not None else self._get_energy_used()

            self.state = "Unemptied"
            self._set_state_entity(
                state="Unemptied",
                attributes=self._unemptied_merged_attributes(run_minutes, energy_used),
                replace=True,
            )

            if self.poll_timer:
                self._safe_cancel_timer(self.poll_timer)
                self.poll_timer = None

            # Cancel running watchdog; start unemptied watchdog only if timeout > 0 (0 = can stay Unemptied indefinitely)
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None
            if self.unemptied_timeout_hours > 0:
                self.unemptied_watchdog_timer = self.run_in(
                    self._unemptied_watchdog_timeout,
                    int(self.unemptied_timeout_hours * 3600)
                )

            self.log(f"State -> Unemptied (ran {run_minutes:.1f} min, used {energy_used:.3f} kWh)", level="INFO")
            self._strict_start_until_door_or_sustain = True

            # Send notification - dishwasher done, please empty!
            # Skip when skip_announce=True (user opened door before we detected finish).
            if not skip_announce and self.sonos_notifier and not self.notification_sent:
                try:
                    self.sonos_notifier.notify(message="Dishwasher is ready to be emptied")
                    self.log("Sent notification.", level="INFO")
                    self.notification_sent = True
                except Exception as e:
                    self.log(f"Error sending notification: {e}", level="ERROR")

    def _handle_force_emptied(self, event_name, data, kwargs):
        """Handle dishwasher_force_emptied: user already opened door / is emptying; skip Unemptied wait."""
        data = data or {}
        st = self.get_state(self.state_entity)
        if st == "Emptied":
            self.log("Force Emptied ignored: already Emptied", level="DEBUG")
            return
        if st == "Off":
            self.log("Force Emptied ignored: already Off", level="WARNING")
            return
        if st not in ("Unemptied", "Paused"):
            self.log(
                f"Force Emptied from {st!r} — use force_unemptied first if the wash may still be running",
                level="WARNING",
            )
            return
        if not self.start_time:
            self._restore_cycle_tracking_from_entity()
        reason = data.get("reason") or "User confirmed emptied (door opened earlier)"
        self.log(f"Force Emptied via event ({reason})", level="INFO")
        self._transition_to_emptied(reason)

    def _handle_force_off(self, event_name, data, kwargs):
        """Handle dishwasher_force_off event: transition to Off (e.g. user requested from script/UI)."""
        if self.get_state(self.state_entity) != "Off":
            self.log("Force Off requested via event -> Off", level="INFO")
            self._safe_cancel_timer(self.emptied_timeout_timer)
            self.emptied_timeout_timer = None
            self._transition_to_off("User requested (force off)", force=True)

    def _handle_force_unemptied(self, event_name, data, kwargs):
        """Handle dishwasher_force_unemptied: cycle finished, door still closed, but state is wrong (e.g. stuck Off/Running)."""
        data = data or {}
        st = self.get_state(self.state_entity)
        if st == "Unemptied":
            self.log("Force Unemptied ignored: already Unemptied", level="DEBUG")
            return
        if st == "Emptied":
            self.log(
                "Force Unemptied ignored: state is Emptied (door open). Close door to go Off, or fix manually.",
                level="WARNING",
            )
            return
        eco = self._get_profile("eco")
        default_min = float(eco.get("duration_min", 227))
        try:
            run_minutes = float(data.get("run_minutes", default_min))
        except (TypeError, ValueError):
            run_minutes = default_min
        energy_used = data.get("energy_kwh")
        if energy_used is not None:
            try:
                energy_used = float(energy_used)
            except (TypeError, ValueError):
                energy_used = 0.65
        else:
            energy_used = 0.65
        skip_raw = data.get("skip_notify", True)
        if isinstance(skip_raw, str):
            skip_announce = skip_raw.lower() in ("1", "true", "yes", "on")
        else:
            skip_announce = bool(skip_raw)

        self.log(
            f"Force Unemptied via event (run_minutes={run_minutes:.0f}, energy_kwh={energy_used}, skip_announce={skip_announce})",
            level="INFO",
        )
        self._safe_cancel_timer(self.emptied_timeout_timer)
        self.emptied_timeout_timer = None
        self._reset_cycle_tracking()
        self._transition_to_unemptied(
            skip_announce=skip_announce,
            run_minutes=run_minutes,
            energy_used=energy_used,
            force=True,
        )

    def _emptied_timeout(self, kwargs):
        """Called when we've been in Emptied for emptied_timeout_minutes; go to Off so we don't stay stuck."""
        if self.get_state(self.state_entity) == "Emptied":
            self.log("Emptied timeout - assuming emptying complete -> Off", level="INFO")
            self._transition_to_off("Emptied timeout - assuming emptying complete")
        self.emptied_timeout_timer = None

    def _revert_emptied_to_unemptied(self):
        """Revert to Unemptied when door was closed too soon (peek / didn't know it had run)."""
        self.state = "Unemptied"
        full = self.get_state(self.state_entity, attribute="all") or {}
        attrs = dict((full.get("attributes") or {}))
        attrs.pop("reason", None)
        if "run_time_minutes" not in attrs and "energy_used" not in attrs:
            run_min = self._get_run_duration_minutes()
            energy = self._get_energy_used()
            if run_min > 0:
                attrs["run_time_minutes"] = round(run_min, 1)
            if energy > 0:
                attrs["energy_used"] = round(energy, 3)
        if self.short_entity:
            attrs["short_selected"] = self.get_state(self.short_entity)
        self._set_state_entity( state="Unemptied", attributes=attrs, replace=True)
        self._safe_cancel_timer(self.unemptied_watchdog_timer)
        self.unemptied_watchdog_timer = None
        if self.unemptied_timeout_hours > 0:
            self.unemptied_watchdog_timer = self.run_in(
                self._unemptied_watchdog_timeout,
                int(self.unemptied_timeout_hours * 3600),
            )
        self._clear_last_cycle_emptied_ts()
        self._strict_start_until_door_or_sustain = True
        self.log("Door closed too soon after opening (peek) -> back to Unemptied", level="INFO")

    def _clear_last_cycle_emptied_ts(self):
        """Remove emptied_ts from last cycle in feedback (user didn't actually empty)."""
        import json
        if not os.path.exists(self.feedback_file):
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = json.load(f)
        except Exception:
            return
        cycles = data.get("cycles") or []
        if not cycles or "emptied_ts" not in cycles[-1]:
            return
        cycles[-1].pop("emptied_ts", None)
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _transition_to_emptied(self, reason):
        """Transition to Emptied state (door open, user is emptying). Log emptied, then auto-transition to Off after timeout."""
        if self._should_change_state("Emptied", force=True):  # Door event bypasses cooling
            energy_used = self._get_energy_used()
            run_minutes = self._get_run_duration_minutes()

            self.state = "Emptied"
            attributes = {
                "reason": reason,
                "run_time_minutes": round(run_minutes, 1) if run_minutes > 0 else None
            }
            if energy_used > 0:
                attributes["energy_used"] = round(energy_used, 3)

            self._set_state_entity( state="Emptied", attributes=attributes)
            
            # Cancel unemptied watchdog since we're now emptying
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None

            # When we entered Emptied (for min_emptying_seconds: door closed too soon = peek, revert to Unemptied)
            self.emptied_at = self._now_utc()
            # Log that user emptied (for analytics); then auto-transition to Off after timeout if door close not seen
            self._mark_last_cycle_emptied()
            self._safe_cancel_timer(self.emptied_timeout_timer)
            self.emptied_timeout_timer = self.run_in(
                self._emptied_timeout,
                self.emptied_timeout_minutes * 60
            )
            
            self.log(f"State -> Emptied ({reason})", level="INFO")

    def _reset_cycle_tracking(self):
        """Reset all cycle-related tracking variables."""
        if getattr(self, "_start_candidate_active", False):
            self._cancel_start_candidate("cycle_reset", level="DEBUG")
        self.start_time = None
        self.energy_start = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False
        self.pause_from_low_power = False
        self.program_timer = None
        self.expected_dur_at_start = None
        self.detected_programme = "unknown"
        self.detected_short = False
        self.detected_quick_short = False
        self.max_power_w = 0.0
        self._last_high_power_time = None
        self.low_power_counter = 0
        self.high_power_counter = 0
        self.pending_start_used_fast_path = False
        self.door_fast_start_armed_until = None
        self.last_door_closed_at = None
        self._sustain_start_begin = None

        if self.poll_timer:
            self._safe_cancel_timer(self.poll_timer)
            self.poll_timer = None
        if self.classify_timer:
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None

        if self.pause_timer:
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None
        if self.pause_finish_timer:
            self._safe_cancel_timer(self.pause_finish_timer)
            self.pause_finish_timer = None

        if self.low_power_timer:
            self._safe_cancel_timer(self.low_power_timer)
            self.low_power_timer = None

        if self.running_watchdog_timer:
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
        if self.unemptied_watchdog_timer:
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None
        if self.emptied_timeout_timer:
            self._safe_cancel_timer(self.emptied_timeout_timer)
            self.emptied_timeout_timer = None
        self.emptied_at = None
        self._safe_cancel_timer(self.power_unavailable_error_timer)
        self.power_unavailable_error_timer = None

    def _cancel_power_unavailable_grace(self):
        """Power readings are valid again; cancel pending Error transition."""
        self._safe_cancel_timer(self.power_unavailable_error_timer)
        self.power_unavailable_error_timer = None

    def _begin_power_unavailable_grace(self, new_label):
        """Do not jump to Off on a brief plug dropout; schedule Error only if outage lasts."""
        if self.get_state(self.state_entity) == "Error":
            return
        if self.power_unavailable_error_timer and self.timer_running(self.power_unavailable_error_timer):
            return
        self.power_unavailable_error_timer = self.run_in(
            self._power_unavailable_error_timeout,
            self.power_unavailable_error_after_seconds,
        )
        self.log(
            f"Power sensor {new_label!r}: waiting {self.power_unavailable_error_after_seconds}s; "
            f"still bad -> Error (short dropout ignored)",
            level="WARNING",
        )

    def _power_unavailable_error_timeout(self, kwargs):
        self.power_unavailable_error_timer = None
        ps = self.get_state(self.power_sensor)
        if ps not in ("unknown", "unavailable", None):
            return
        self._transition_to_error(
            f"Power sensor unavailable >= {self.power_unavailable_error_after_seconds}s",
            error_key="power_sensor",
        )

    def _push_mobile(self, message):
        """Page the phone (plug outage / recovery) - same pattern as gw2000a_watchdog."""
        try:
            notifier = self.get_app("MobileNotifier")
            if notifier is None:
                self.log("MobileNotifier app not found - cannot push", level="WARNING")
                return
            self.create_task(notifier.notify(title="Dishwasher", message=message, target=self.notify_target))
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")

    def _error_merged_attributes(self, reason, error_key):
        try:
            full = self.get_state(self.state_entity, attribute="all") or {}
            existing = dict((full.get("attributes") or {}))
        except Exception:
            existing = {}
        existing["reason"] = reason
        existing["error"] = error_key
        return existing

    def _transition_to_error(self, reason, error_key="power_sensor"):
        """Long power sensor outage or fatal sensor issue — user should fix plug/HA then force_off / force_unemptied."""
        if self.get_state(self.state_entity) == "Error":
            return
        if not self._should_change_state("Error", force=True):
            return
        was_running = self.get_state(self.state_entity) == "Running"
        self.state = "Error"
        self._set_state_entity(state="Error", attributes=self._error_merged_attributes(reason, error_key), replace=True)
        self.log(f"State -> Error ({reason})", level="WARNING")
        # One push per outage by construction (already-Error returns above).
        self._plug_error_pushed = True
        self._push_mobile(
            f"{reason} - cycle monitoring is blind (state -> Error). Check the plug/WiFi; "
            f"the state auto-resets to Off once power readings return."
        )
        self._reset_cycle_tracking()
        if was_running:
            self._strict_start_until_door_or_sustain = True

    def _power_changed(self, entity, attr, old, new, kwargs):
        try:
            watts = float(new or 0)
        except (ValueError, TypeError):
            self.log(f"Non-numeric power reading: {new}", level="WARNING")
            return

        self._cancel_power_unavailable_grace()
        self._record_power_reading(watts)
        current_state = self.get_state(self.state_entity)
        if current_state == "Error":
            self._transition_to_off("Power sensor OK after Error (verify dishwasher)", force=True)
            if self._plug_error_pushed:
                self._plug_error_pushed = False
                self._push_mobile(
                    "Power plug is reporting again - state reset to Off. A cycle may have "
                    "run unseen; verify the dishwasher."
                )
            current_state = self.get_state(self.state_entity)
        if current_state == "Error":
            return

        # Track max power and last high-power time (for drying-phase detection)
        if current_state == "Running":
            if watts > self.max_power_w:
                self.max_power_w = watts
            if watts >= 50:  # Heating/pump; used to detect "we've left the drying phase" vs "still in drying"
                self._last_high_power_time = self._now_utc()

        # Recovery: power high again after Unemptied
        if current_state == "Unemptied" and watts >= self.start_w:
            self._recover_from_false_unemptied(watts)
            return

        # High power branch (start detection)
        if watts >= self.start_w:
            self.low_power_counter = 0
            now = self._now_utc()
            armed = (
                self.door_fast_start_armed_until is not None
                and now <= self.door_fast_start_armed_until
            )
            effective_threshold = 1 if armed else self.high_power_threshold

            if current_state == "Off" and self._strict_start_until_door_or_sustain and not armed:
                # Wash finished but user had not opened the door (or plug went unavailable): ignore short plug blips
                if self._sustain_start_begin is None:
                    self._sustain_start_begin = now
                    self.log(
                        f"strict_start: need {self.start_sustain_seconds_without_door}s sustained power >= {self.start_w}W "
                        f"or door open / door-close arm before next Running",
                        level="DEBUG",
                    )
                elapsed_s = (now - self._sustain_start_begin).total_seconds()
                if elapsed_s >= self.start_sustain_seconds_without_door:
                    self.pending_start_used_fast_path = False
                    try:
                        w_sustain = float(self.get_state(self.power_sensor) or 0)
                    except (TypeError, ValueError):
                        w_sustain = 0.0
                    self._feed_off_to_running_start_candidate(w_sustain, force_commit=True)
            else:
                self.high_power_counter += 1
                if self.high_power_counter >= effective_threshold:
                    if current_state == "Off":
                        self.pending_start_used_fast_path = bool(armed)
                        self._feed_off_to_running_start_candidate(watts, force_commit=False)
                    elif current_state == "Paused":
                        # Machine resumed after door closed - restore Running in UI (confirmed programme + remaining time)
                        self._transition_to_running_from_pause()
        else:
            self.high_power_counter = 0
            if current_state == "Off" and self._sustain_start_begin is not None:
                self.log("strict_start: sustain timer reset (power dropped below start_w)", level="DEBUG")
            self._sustain_start_begin = None
            if self._start_candidate_active:
                self._maybe_schedule_start_candidate_idle(watts)

        # Low power branch (finish detection) - only while door is closed
        if current_state == "Running" and watts <= self.stop_w:
            if not self.low_power_timer:
                self.log(f"Power <= {self.stop_w}W -> may be finished, starting {self.stop_for}s timer", level="INFO")
                self.low_power_timer = self.run_in(self._confirm_finished, self.stop_for)
        elif current_state == "Running" and watts > self.stop_w:
            # Power recovered - cancel finish timer
            if self.low_power_timer:
                self._safe_cancel_timer(self.low_power_timer)
                self.low_power_timer = None
                self.log(f"Power recovered to {watts}W, cancelled finish timer", level="DEBUG")

    def _poll_power(self, kwargs):
        """Conditional polling"""
        current_state = self.get_state(self.state_entity)

        if current_state not in ("Running", "Paused"):
            if self.poll_timer:
                self._safe_cancel_timer(self.poll_timer)
                self.poll_timer = None
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

        self._power_changed(self.power_sensor, None, None, watts, {})
        self.poll_timer = self.run_in(self._poll_power, 60)

    def _tick_classify(self, kwargs):
        """Periodic classification and attribute update."""
        if self.get_state(self.state_entity) not in ("Running", "Paused"):
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None
            return
        self._update_running_attributes()
        self.classify_timer = self.run_in(self._tick_classify, 30)

    def _confirm_running(self, kwargs):
        """Legacy entry point; Off->Running now uses start-candidate feeds. Kept for compatibility."""
        current_power_state = self.get_state(self.power_sensor)
        if current_power_state in ["unknown", "unavailable"]:
            self.pending_start_used_fast_path = False
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        try:
            watts_confirm = float(current_power_state or 0)
        except (ValueError, TypeError):
            self.pending_start_used_fast_path = False
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        if watts_confirm >= self.start_w:
            st = self.get_state(self.state_entity)
            if st == "Off":
                self._feed_off_to_running_start_candidate(watts_confirm, force_commit=False)
            elif st == "Unemptied":
                self._recover_from_false_unemptied(watts_confirm)
            else:
                self.pending_start_used_fast_path = False
        else:
            self.pending_start_used_fast_path = False

    def _confirm_finished(self, kwargs):
        """Confirm the cycle has finished - power dropped, door still closed. Full-duration guard (no finish during drying)."""
        current_state = self.get_state(self.state_entity)

        if current_state != "Running":
            self.log(f"_confirm_finished: state is {current_state!r}, not Running - skipping", level="DEBUG")
            self.low_power_timer = None
            return

        current_power_state = self.get_state(self.power_sensor)
        if current_power_state in ["unknown", "unavailable"]:
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        try:
            watts_confirm = float(current_power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        if watts_confirm <= self.stop_w:
            run_min = self._get_run_duration_minutes()
            classified = self._classify_programme()
            display_prog = self._get_programme_for_display()
            guard_dur = self._get_guard_duration(tick_prog=display_prog)
            # ECO has a long drying phase (0–3 W) before the real end. Require at least 95% of programme
            # duration so we don't trigger during drying; allow real finishes that end a few min early (e.g. 230 vs 234).
            min_run_to_accept = guard_dur * 0.95
            if run_min < min_run_to_accept:
                self.log(
                    f"Power-based: run {run_min:.0f}min < {min_run_to_accept:.0f}min (95% of {guard_dur:.0f}min) - blocking (drying phase)",
                    level="INFO",
                )
                self.low_power_timer = None
                return
            if self._is_valid_completed_cycle():
                run_minutes_wall = run_min
                run_minutes, duration_source = self._correct_duration(run_minutes_wall)
                idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                energy_kwh = self._get_energy_used()
                self._transition_to_unemptied(skip_announce=False, run_minutes=run_minutes, energy_used=energy_kwh)
                confirmed, is_human = self._get_confirmed_from_selector(classified)
                self._save_cycle_feedback(
                    predicted=classified,
                    confirmed=confirmed,
                    duration_min=run_minutes,
                    energy_kwh=energy_kwh,
                    max_power_w=self.max_power_w,
                    programme_confirmed_by_human=is_human,
                    duration_source=duration_source,
                    end_reason="low_power_detected",
                    idle_min=idle_min,
                )
            else:
                run_min = self._get_run_duration_minutes()
                energy_used = self._get_energy_used()
                self.log(
                    f"Cycle validation failed: run {run_min:.0f}min (min {self.min_cycle_minutes}), "
                    f"energy {energy_used:.2f}kWh (min {self.min_energy_kwh}) - not saving feedback",
                    level="INFO",
                )
        
        self.low_power_timer = None

    def _handle_unavailable(self, entity, attribute, old, new, kwargs):
        """Handle entity becoming unavailable or unknown."""
        if entity == self.power_sensor:
            self._begin_power_unavailable_grace(str(new))
            return
        if entity == self.state_entity:
            self.log(
                f"State entity became {new!r} — UI may be stale until entity is back",
                level="WARNING",
            )
