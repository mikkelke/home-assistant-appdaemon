"""
DryerMonitor - Tracks dryer state with power monitoring, programme estimation, and keep-fresh detection.

Appliance: Miele TCB150 WP (Heat Pump Dryer)
- Programme classification from energy + runtime (dryer_programmes.yaml), not power-range.
- 80% duration guard before declaring Unemptied; feedback file and learning from confirmed cycles.
- Fill window: door open with low power before fill_window_minutes = add laundry (Off).
- Finish uld (5 min, 0.02 kWh) is not a real cycle (is_real: false).

States:
    - Off: Dryer is idle, empty, or emptied
    - Running: Drying cycle is in progress
    - Paused: Door opened during Running with power still high (checking laundry)
    - Unemptied: Cycle completed (power dropped), door still closed - waiting for user
    - Emptied: Door opened after cycle complete - user is emptying (door still open)

State Transitions:
    - Power goes high while Off -> Running
    - Power drops while Running (door closed) -> Unemptied (reminder to empty)
    - Keep-fresh pattern detected -> Unemptied (main cycle done)
    - Door opens while Running + power LOW -> Emptied (user is emptying it)
    - Door opens while Running + power HIGH -> Paused (checking laundry)
    - Door closes from Paused + power HIGH -> Running (cycle resumes)
    - Door closes from Paused + power LOW + valid cycle -> Unemptied
    - Door closes from Paused + power LOW + invalid cycle -> Off
    - Door opens while Unemptied -> Emptied (user is emptying)
    - Door closes while Emptied -> Off (emptying complete)
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import os
import time
import yaml
from datetime import datetime, timedelta, timezone
import statistics


class DryerMonitor(hass.Hass):
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
        self.start_w = float(self.args["start_w"])
        self.stop_w = float(self.args["stop_w"])
        self.run_for = int(self.args["run_for"])
        self.stop_for = int(self.args["stop_for"])
        self.door_close_fast_start_window_s = int(self.args.get("door_close_fast_start_window_s", 600))
        self.door_close_fast_confirm_s = int(self.args.get("door_close_fast_confirm_s", 12))
        # Split selectors: programme, dryness, Skåne+, time (time only for Varm luft). App derives flat key.
        self.programme_entity = self.args.get("programme_entity")
        self.dryness_entity = self.args.get("dryness_entity")
        self.skane_plus_entity = self.args.get("skane_plus_entity")
        self.time_minutes_entity = self.args.get("time_minutes_entity")
        # Reset split selectors when returning to Off so each new cycle starts unconfirmed
        self.reset_programme_on_idle = bool(self.args.get("reset_programme_on_idle", True))
        self.programme_unconfirmed_option = self.args.get(
            "programme_unconfirmed_option", "Auto (unconfirmed)"
        )
        self.dryness_unselected_option = self.args.get("dryness_unselected_option", "—")
        self.reset_time_minutes_on_idle = bool(self.args.get("reset_time_minutes_on_idle", False))
        self.time_minutes_idle_default = self.args.get("time_minutes_idle_default", "20")
        # Announce when finished (match washer): toggle off to silence; app resets to on when Emptied
        self.announce_entity = self.args.get("announce_entity")
        self.announce_message = self.args.get("announce_message", "Dryer is ready to be emptied")

        # Programmes from YAML (replaces programs/power_range for classification)
        self._load_programme_profiles()

        # Feedback and learning
        self.feedback_file = self.args.get("feedback_file") or os.path.join(
            os.path.dirname(__file__), "dryer_feedback.json"
        )
        self._learned_durations = {}
        self._load_and_apply_feedback()

        # Cycle validation and fill window
        self.min_cycle_minutes = int(self.args.get("min_cycle_minutes", 60))
        self.min_energy_kwh = float(self.args.get("min_energy_kwh", 0.05))
        self.fill_window_minutes = int(self.args.get("fill_window_minutes", 60))
        self.pause_timeout_minutes = int(self.args.get("pause_timeout_minutes", 10))

        # History correction: threshold for "high power" in energy series (W)
        self.energy_active_watts = float(self.args.get("energy_active_watts", 100.0))

        # Safety watchdogs - prevent stuck states
        self.max_running_hours = float(self.args.get("max_running_hours", 5))
        self.unemptied_timeout_hours = float(self.args.get("unemptied_timeout_hours", 24))

        # State tracking
        self.low_power_timer = None
        self.start_time = None
        self.energy_start = None
        self.poll_timer = None

        # Power pattern detection for keep-fresh
        self.power_readings = []
        self.pattern_check_timer = None
        self.keep_fresh_detected = False
        self.power_reading_interval = 15
        self.pattern_window = 10

        # State stability
        self.last_state_change = None
        self.cooling_period = int(self.args.get("cooling_period", 600))
        self.main_cycle_power = float(self.args.get("main_cycle_power", 400))
        self.high_power_counter = 0
        self.high_power_threshold = int(self.args.get("high_power_threshold", 5))
        self.notification_sent = False

        # Pause state tracking
        self.pause_timer = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False

        # Watchdog timers
        self.running_watchdog_timer = None
        self.unemptied_watchdog_timer = None

        # Programme classification and guard
        self.expected_dur_at_start = None
        self.detected_programme = "unknown"
        self.max_power_w = 0.0
        self.classify_timer = None
        self.door_fast_start_armed_until = None
        self.start_confirm_timer = None

        # Restore previous state
        existing = self.get_state(self.state_entity)
        valid_states = ("Running", "Unemptied", "Paused", "Emptied")
        self.state = existing if existing in valid_states else "Off"
        self._set_state_entity( state=self.state)

        # If we restored Running/Paused, restore start_time and timers so we keep polling
        # and can detect power drop (fixes stuck "Running" after AppDaemon restart).
        if self.state in ("Running", "Paused"):
            self._restore_running_state()

        # Listen for events
        self.listen_state(self._power_changed, self.power_sensor)
        self.listen_state(self._door_state_changed, self.door_sensor)
        self.listen_state(self._handle_unavailable, self.state_entity, new="unavailable")
        self.listen_state(self._handle_unavailable, self.power_sensor, new="unavailable")

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

        # Bootstrap
        current_power = self.get_state(self.power_sensor)
        if current_power not in ["unknown", "unavailable"]:
            try:
                current_watts = float(current_power or 0)
                self._power_changed(self.power_sensor, None, None, current_watts, {})
            except (ValueError, TypeError):
                self._handle_unavailable(self.power_sensor, None, None, current_power, {})

        self.log(f"DryerMonitor (Miele TCB150 WP) initialized - state: {self.state}", level="INFO")

    def _now_utc(self):
        return datetime.fromtimestamp(time.time(), timezone.utc)

    def _local_tz(self):
        return datetime.now().astimezone().tzinfo

    def _format_local(self, dt):
        if dt is None:
            return ""
        tz = self._local_tz()
        return dt.astimezone(tz).isoformat(timespec="seconds") if getattr(dt, "tzinfo", None) else str(dt)

    def _format_utc(self, dt):
        if dt is None:
            return ""
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _parse_utc_iso(self, s):
        """Parse ISO datetime string from entity attribute to UTC datetime."""
        if not s or not isinstance(s, str):
            return None
        try:
            s = s.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    def _restore_running_state(self):
        """After AppDaemon restart: restore start_time and timers when state is Running/Paused.
        Ensures we keep polling power and can detect cycle end (power drop)."""
        if self.state not in ("Running", "Paused"):
            return
        try:
            full = self.get_state(self.state_entity, attribute="all")
            attrs = (full or {}).get("attributes") or {}
            cycle_start = attrs.get("cycle_start_time")
            if cycle_start:
                self.start_time = self._parse_utc_iso(cycle_start)
            if self.start_time is None:
                self.log("Restore Running: no cycle_start_time in entity - start_time left None", level="WARNING")
            else:
                en = attrs.get("energy_at_start")
                if en is not None:
                    try:
                        self.energy_start = float(en)
                    except (ValueError, TypeError):
                        pass
                prog = attrs.get("detected_programme") or "unknown"
                self.detected_programme = prog
                dur = attrs.get("programme_duration_min")
                if dur is not None:
                    try:
                        self.expected_dur_at_start = float(dur)
                    except (ValueError, TypeError):
                        pass
                if self.expected_dur_at_start is None:
                    self.expected_dur_at_start = self._get_guard_duration(tick_prog=prog)
            if not self.poll_timer:
                self.poll_timer = self.run_in(self._poll_power, 60)
            if not self.classify_timer:
                self.classify_timer = self.run_in(self._tick_classify, 30)
            if self.start_time:
                self._update_running_attributes()
            self.log("Restored Running state: start_time and timers resumed", level="INFO")
        except Exception as e:
            self.log(f"Restore Running state failed: {e}", level="WARNING")

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

        # Keep-fresh detection counts as valid
        if self.keep_fresh_detected:
            return True

        is_valid = (run_minutes >= self.min_cycle_minutes and
                    energy_used >= self.min_energy_kwh)

        self.log(f"Cycle validation: {run_minutes:.1f} min (need {self.min_cycle_minutes}), "
                 f"{energy_used:.3f} kWh (need {self.min_energy_kwh}) -> {'valid' if is_valid else 'invalid'}",
                 level="DEBUG")

        return is_valid

    def _get_profile(self, programme: str):
        """Return profile dict for programme."""
        return DryerMonitor.PROGRAMME_PROFILES.get(
            programme, DryerMonitor.PROGRAMME_PROFILES.get("unknown", {})
        )

    def _load_programme_profiles(self):
        """Load programme profiles from dryer_programmes.yaml. Minimal defaults; all keys from YAML."""
        prog_file = self.args.get("programmes_file") or os.path.join(
            os.path.dirname(__file__), "dryer_programmes.yaml"
        )
        defaults = {
            "unknown": {"label": "Unknown", "duration_min": 120, "max_energy_kwh": 2.0},
            "finish_uld": {"label": "Finish uld", "duration_min": 5, "max_energy_kwh": 0.02, "is_real": False},
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
                DryerMonitor.PROGRAMME_PROFILES = merged
                self.log(f"Loaded {len(profiles)} programme profiles from {prog_file}", level="INFO")
            else:
                DryerMonitor.PROGRAMME_PROFILES = defaults
        except FileNotFoundError:
            DryerMonitor.PROGRAMME_PROFILES = defaults
            self.log(f"Programme file {prog_file} not found - using defaults", level="WARNING")
        except Exception as exc:
            DryerMonitor.PROGRAMME_PROFILES = defaults
            self.log(f"Failed to load {prog_file}: {exc} - using defaults", level="ERROR")

    def _load_and_apply_feedback(self):
        """Load dryer_feedback.json and apply learned programme data. Only confirmed cycles."""
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
            key = prog
            prev = self._learned_durations.get(key, {"n": 0, "avg": float(dur)})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + float(dur)) / n_new
            self._learned_durations[key] = {"n": n_new, "avg": avg_new}
        if self._learned_durations:
            self.log(f"Loaded learned durations for {list(self._learned_durations.keys())}", level="INFO")

    def _classify_programme(self):
        """Classify programme from energy + runtime (heat pump: use energy and time, not power bands)."""
        if not self.start_time:
            return "unknown"
        run_min = (self._now_utc() - self.start_time).total_seconds() / 60
        energy = self._get_energy_used()
        if run_min < 5:
            return "unknown"
        # Finish uld: ~0.02 kWh, ~5 min - not a real cycle
        if energy < 0.05 and run_min < 10:
            return "finish_uld"
        # Short real cycles: 0.46–0.50 kWh, 60–66 min (skjorter, strygelet, finvask)
        if 0.40 <= energy <= 0.55 and 55 <= run_min <= 75:
            if run_min <= 62 and energy <= 0.48:
                return "skjorter__skabstoert"
            if run_min >= 64:
                return "strygelet__skabstoert"
            return "finvask__skabstoert__skane_fixed"
        # Medium: 0.75–0.96 kWh (imprægnering 95 min, ekspres 100 min)
        if 0.70 <= energy <= 1.0 and 90 <= run_min <= 105:
            if energy <= 0.78 and run_min <= 97:
                return "impraegnering__skabstoert"
            return "ekspres__skabstoert"
        # Denim: ~0.95 kWh, ~115 min
        if 0.88 <= energy <= 1.05 and 108 <= run_min <= 125:
            return "denim__skabstoert"
        # Bomuld strygetørt: 0.85–1.25 kWh, 83–120 min
        if 0.80 <= energy <= 1.30 and 80 <= run_min <= 125:
            return "bomuld__strygetoert"
        # Bomuld skabstørt: 1.30–1.50 kWh, 118–140 min
        if 1.25 <= energy <= 1.55 and 115 <= run_min <= 145:
            return "bomuld__skabstoert"
        # Bomuld skabstørt + Skåne: 1.75 kWh, 165 min
        if energy >= 1.65 and run_min >= 155:
            return "bomuld__skabstoert__skane"
        # Bomuld eco: 1.70 kWh, 155 min
        if energy >= 1.55 and run_min >= 140:
            return "bomuld_eco__skabstoert"
        if 0.90 <= energy <= 1.0 and run_min >= 90 and run_min <= 100:
            return "ekspres__skabstoert"
        # Fallbacks by energy
        if energy < 0.5 and run_min < 70:
            return "skjorter__skabstoert"
        if energy < 0.9 and run_min < 110:
            return "ekspres__skabstoert"
        if energy < 1.2:
            return "bomuld__strygetoert"
        if energy < 1.6:
            return "bomuld__skabstoert"
        return "bomuld_eco__skabstoert"

    def _get_programme_duration(self, prog: str, use_learned: bool = True) -> int:
        """Expected duration in minutes. For guards use use_learned=False."""
        profile = self._get_profile(prog)
        manual = profile.get("duration_min")
        if manual is None or manual <= 0:
            manual = 120
        if not use_learned:
            return manual
        learned = self._learned_durations.get(prog)
        if learned is None or learned["n"] < 1:
            return manual
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
        dur = profile.get("duration_min")
        if dur is None or dur <= 0:
            return int(self.max_running_hours * 60)
        return dur

    def _get_guard_duration(self, tick_prog=None):
        """Duration for 80% guard. Use manual duration only."""
        if tick_prog and tick_prog != "unknown":
            d = self._get_programme_duration(tick_prog, use_learned=False)
            if d:
                return d
        if self.expected_dur_at_start is not None:
            return self.expected_dur_at_start
        return self._programme_max_duration_minutes(classification=tick_prog)

    def _update_running_attributes(self):
        """Update state entity with progress/ETA attributes while Running."""
        if self.get_state(self.state_entity) not in ("Running", "Paused") or not self.start_time:
            return
        prog = self._classify_programme()
        self.detected_programme = prog
        # Effective key: user-selected (derived from selectors) or classified
        effective_key = self._get_confirmed_programme_key() or prog
        profile = self._get_profile(effective_key)
        label = profile.get("label", effective_key)
        dur = self._get_programme_duration(effective_key)
        elapsed = (self._now_utc() - self.start_time).total_seconds() / 60
        remaining = max(0, dur - elapsed)
        progress = min(100, round(100 * elapsed / dur)) if dur > 0 else 0
        eta = self.start_time + timedelta(minutes=dur) if dur else None
        attrs = {
            "detected_programme": prog,
            "derived_programme_key": effective_key,
            "programme_label": label,
            "cycle_start_time": self._format_utc(self.start_time),
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
            # elapsed_minutes/progress_pct/energy_used silently drop from published attributes
            # whenever they're 0/0.0 (every cycle's first tick(s)); estimated_remaining_min drops
            # the same way once a cycle overruns its estimate -- AppDaemon 4.5.13 set_state bug,
            # not ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity( state=self.get_state(self.state_entity), attributes=existing, replace=True)
        except Exception:
            pass

    def _attrs_clear_running_progress(self):
        """Clear Running-cycle progress fields so the UI does not show stale ETA/progress after Unemptied/Emptied.
        Match washer: use \"\" / None so HA replaces values (partial updates can leave old attrs)."""
        return {
            "cycle_start_time": "",
            "started_at_display": "",
            "estimated_end_time": "",
            "estimated_remaining_min": None,
            "elapsed_minutes": None,
            "progress_pct": None,
            "programme_duration_min": None,
            "programme_label": "",
            "detected_programme": "",
            "energy_at_start": None,
        }

    def _tick_classify(self, kwargs):
        """Periodic classification and attribute update."""
        if self.get_state(self.state_entity) not in ("Running", "Paused"):
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None
            return
        self._update_running_attributes()
        self.classify_timer = self.run_in(self._tick_classify, 30)

    def _flatten_history(self, hist, entity_id=None):
        """AppDaemon get_history returns list[list[dict]]. Normalize to list[dict]."""
        if isinstance(hist, dict):
            hist = hist.get(entity_id or "", []) or hist.get("history", []) if entity_id else next(iter(hist.values()), [])
        if isinstance(hist, list) and hist and isinstance(hist[0], list):
            return hist[0]
        return hist if isinstance(hist, list) else []

    def _get_programme_duration_hint_for_history(self):
        """Duration hint for history correction: detected programme."""
        if self.detected_programme and self.detected_programme != "unknown":
            return float(self._get_programme_duration(self.detected_programme))
        return None

    def _estimate_cycle_end_from_history(self, expected_duration_min=None):
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
                    t = self._parse_utc(ts)
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

    def _parse_utc(self, s: str):
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

    def _correct_duration(self, run_minutes_wall: float, log_prefix: str = "") -> tuple:
        """Correct wall-clock duration using HA history. Returns (run_minutes, duration_source)."""
        run_minutes = run_minutes_wall
        duration_source = None
        hint = self._get_programme_duration_hint_for_history()
        actual_end = self._estimate_cycle_end_from_history(expected_duration_min=hint)
        if actual_end and self.start_time:
            run_minutes_actual = (actual_end - self.start_time).total_seconds() / 60
            if run_minutes_actual >= self.min_cycle_minutes and run_minutes_actual <= run_minutes:
                delta = run_minutes - run_minutes_actual
                if delta > 1.0:
                    self.log(f"{log_prefix}Using HA history: {run_minutes_actual:.1f} min (detection was {delta:.0f} min late)", level="INFO")
                run_minutes = run_minutes_actual
                duration_source = "history_corrected"
        return (run_minutes, duration_source)

    # Dryness selector value -> internal segment (for derived key)
    _DRYNESS_TO_SEGMENT = {
        "Ekstra tørt": "ekstra_toert",
        "Skabstørt": "skabstoert",
        "Strygetørt": "strygetoert",
        "Rulletørt": "rulletoert",
    }
    # Programmes where user must pick a real dryness (not "—", same convention as washer sub-selectors)
    _PROGRAMMES_REQUIRING_DRYNESS = frozenset(
        ("Finvask", "Udglatning", "Bomuld", "Strygelet", "Skjorter", "Denim", "Ekspres", "Sengetøj")
    )

    def _get_confirmed_programme_key(self) -> str | None:
        """Derive flat programme key from split selectors (programme, dryness, Skåne+, time).
        Returns None if programme is 'Auto (unconfirmed)', dryness is not a real level (when required;
        use '—' like washer temperature/spin), or selectors not configured."""
        if not self.programme_entity:
            return None
        prog = (self.get_state(self.programme_entity) or "").strip()
        if not prog or prog in ("Auto (unconfirmed)", "unknown"):
            return None
        dryness = (self.get_state(self.dryness_entity) or "").strip() if self.dryness_entity else ""
        skane_on = self.get_state(self.skane_plus_entity) == "on" if self.skane_plus_entity else False
        time_val = (self.get_state(self.time_minutes_entity) or "").strip() if self.time_minutes_entity else ""

        # Fixed programmes (no dryness / time choice)
        if prog in ("Bomuld eco", "Bomuld eco - Skabstørt"):
            return "bomuld_eco__skabstoert"
        if prog in ("Finish uld",):
            return "finish_uld"
        if prog in ("Imprægnering",):
            return "impraegnering__skabstoert"

        # Varm luft: time 20–120 in 10-min steps
        if prog in ("Varm luft",):
            try:
                # Allow "90", "90 min", etc.
                digits = "".join(c for c in str(time_val or "90") if c.isdigit())
                min_val = int(digits) if digits else 90
                min_val = max(20, min(120, ((min_val + 5) // 10) * 10))
            except (ValueError, TypeError):
                min_val = 90
            seg = f"varm_luft__{min_val:03d}min"
            return f"{seg}__skane" if skane_on else seg

        if prog not in self._PROGRAMMES_REQUIRING_DRYNESS:
            return None

        # Only the four real levels count (same idea as washer: "—" is not in allowed list)
        if not dryness or dryness not in self._DRYNESS_TO_SEGMENT:
            return None
        dry_seg = self._DRYNESS_TO_SEGMENT[dryness]

        # Programmes with Skåne+ always on (Finvask, Udglatning)
        if prog in ("Finvask",):
            if dry_seg == "strygetoert":
                return "finvask__strygetoert__skane_fixed"
            return "finvask__skabstoert__skane_fixed"
        if prog in ("Udglatning",):
            if dry_seg == "strygetoert":
                return "udglatning__strygetoert__skane_fixed"
            return "udglatning__skabstoert__skane_fixed"

        # Programmes with optional Skåne+ and dryness
        base = None
        if prog in ("Bomuld",):
            base = "bomuld"
        elif prog in ("Strygelet",):
            base = "strygelet"
        elif prog in ("Skjorter",):
            base = "skjorter"
        elif prog in ("Denim",):
            base = "denim"
        elif prog in ("Ekspres",):
            base = "ekspres"
            skane_on = False  # Ekspres has no Skåne+
        elif prog in ("Sengetøj",):
            base = "sengetoej"
            skane_on = False  # Sengetøj has no Skåne+
        if base:
            key = f"{base}__{dry_seg}"
            if skane_on:
                key += "__skane"
            return key

        return None

    def _get_confirmed_from_selector(self, predicted: str) -> tuple:
        """Return (confirmed_programme_key, programme_confirmed_by_human). Uses derived key from split selectors."""
        key = self._get_confirmed_programme_key()
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
        """Append one cycle record to dryer_feedback.json."""
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
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write feedback {self.feedback_file}: {e}", level="WARNING")
            return
        if programme_confirmed_by_human:
            prev = self._learned_durations.get(confirmed, {"n": 0, "avg": duration_min})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + duration_min) / n_new
            self._learned_durations[confirmed] = {"n": n_new, "avg": avg_new}
        self.log(f"Feedback saved: {confirmed} duration {duration_min:.0f}min energy {energy_kwh:.2f}kWh", level="INFO")

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
        """Handle door opening. Fill window: before fill_window_minutes, door+low power = Off (add laundry)."""
        current_power = self._get_current_power()
        elapsed = self._get_run_duration_minutes()
        self.log(f"Door opened, state: {current_state}, power: {current_power:.1f}W, elapsed: {elapsed:.1f}min", level="DEBUG")

        if current_state in ("Off", "Emptied"):
            self.door_fast_start_armed_until = None

        if current_state == "Running":
            if current_power <= self.stop_w or self.keep_fresh_detected:
                # Low power - cycle finished or interrupted
                if elapsed < self.fill_window_minutes:
                    self.log(f"Door opened with low power before {self.fill_window_minutes}min - Off (add laundry/interrupted)", level="INFO")
                    self._transition_to_off("Door opened before fill window - add laundry or interrupted")
                elif not self._is_valid_completed_cycle():
                    self._transition_to_off("Door opened - cycle invalid or incomplete")
                else:
                    prog = self._classify_programme()
                    effective_key = self._get_confirmed_programme_key() or prog
                    guard_dur = self._get_guard_duration(tick_prog=effective_key)
                    if elapsed < guard_dur * 0.8:
                        self.log(f"Door open: run {elapsed:.0f}min < 80% of {guard_dur:.0f}min - treating as incomplete", level="DEBUG")
                        self._transition_to_off("Door opened - cycle not past 80% guard")
                    else:
                        self.log(f"Door opened with low power ({current_power:.1f}W) - user is emptying -> Unemptied then Emptied", level="INFO")
                        run_minutes_wall = elapsed
                        run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Door-open ")
                        idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                        energy_kwh = self._get_energy_used()
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
                # Power is HIGH - user is checking laundry mid-cycle
                self.door_opened_time = datetime.now()
                self.door_opened_during_cycle = True
                self._transition_to_paused()

        elif current_state == "Paused":
            self.door_opened_time = datetime.now()

        elif current_state == "Unemptied":
            # Door opened after notification - user is emptying
            self._transition_to_emptied("Door opened - emptying")

    def _handle_door_closed(self, current_state):
        """Handle door closing event."""
        self.log(f"Door closed, current state: {current_state}", level="DEBUG")

        if current_state == "Paused":
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None

            current_power = self._get_current_power()

            if current_power >= self.start_w:
                self._transition_to_running_from_pause()
            else:
                self._evaluate_pause_exit()
        
        elif current_state == "Emptied":
            # Door closed after emptying - cycle complete, go to Off
            self.log(f"Door closed after emptying -> Off", level="INFO")
            self._transition_to_off("Door closed - emptying complete")
            now = self._now_utc()
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

        elif current_state == "Off":
            now = self._now_utc()
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

    def _transition_to_paused(self):
        """Transition to Paused state when door opens during Running with high power."""
        if self._should_change_state("Paused"):
            self.state = "Paused"
            # keep_fresh_detected silently drops from published attributes whenever it's False
            # (always False on this path -- Paused excludes the keep-fresh branch) -- AppDaemon
            # 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity(
                state="Paused",
                attributes={
                    "reason": "Door opened during cycle (checking laundry)",
                    "run_time_minutes": round(self._get_run_duration_minutes(), 1),
                    "energy_used": round(self._get_energy_used(), 3),
                    "keep_fresh_detected": self.keep_fresh_detected
                }
            )
            self.log(f"State -> Paused (checking laundry mid-cycle)", level="INFO")

            # Timeout
            self.pause_timer = self.run_in(
                self._pause_timeout,
                self.pause_timeout_minutes * 60
            )

    def _pause_timeout(self, kwargs):
        """Called when door has been open for too long during a pause."""
        current_state = self.get_state(self.state_entity)

        if current_state == "Paused":
            self.log(f"Pause timeout - assuming emptied", level="INFO")
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
            self._transition_to_off(f"Watchdog: unemptied timeout ({self.unemptied_timeout_hours}h)")
        self.unemptied_watchdog_timer = None

    def _transition_to_running_from_pause(self):
        """Resume Running state after pause."""
        if self._should_change_state("Running"):
            self.state = "Running"
            self._set_state_entity( state="Running")
            self.door_opened_time = None
            self.log("State -> Running (resumed after pause)", level="INFO")

            if not self.poll_timer:
                self.poll_timer = self.run_in(self._poll_power, 60)

    def _evaluate_pause_exit(self):
        """Determine whether to go to Unemptied or Off when exiting Paused state."""
        if self._is_valid_completed_cycle():
            run_minutes_wall = self._get_run_duration_minutes()
            run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Pause-exit ")
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

    def _reset_programme_selectors_to_unconfirmed(self):
        """Set programme / dryness / Skåne+ / time to idle defaults (user must confirm next cycle)."""
        if not self.reset_programme_on_idle:
            return
        try:
            if self.programme_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.programme_entity,
                    option=self.programme_unconfirmed_option,
                )
            if self.dryness_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.dryness_entity,
                    option=self.dryness_unselected_option,
                )
            if self.skane_plus_entity:
                self.call_service("input_boolean/turn_off", entity_id=self.skane_plus_entity)
            if self.reset_time_minutes_on_idle and self.time_minutes_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.time_minutes_entity,
                    option=self.time_minutes_idle_default,
                )
            self.log("Programme selectors reset to unconfirmed / defaults for next cycle", level="DEBUG")
        except Exception as e:
            self.log(f"Could not reset programme selectors: {e}", level="WARNING")

    def _transition_to_off(self, reason):
        """Transition to Off state."""
        if self._should_change_state("Off"):
            self.state = "Off"
            self._set_state_entity( state="Off")
            self.log(f"State -> Off ({reason})", level="INFO")
            self._reset_programme_selectors_to_unconfirmed()
            self._reset_cycle_tracking()

    def _transition_to_unemptied(self, skip_announce=False, run_minutes=None, energy_used=None):
        """Transition to Unemptied state (cycle done, door still closed, waiting for user).

        skip_announce: If True, do not send the "dryer ready to empty" notification.
        run_minutes, energy_used: Optional corrected values (from history correction).
        """
        if self._should_change_state("Unemptied"):
            run_minutes = run_minutes if run_minutes is not None else self._get_run_duration_minutes()
            energy_used = energy_used if energy_used is not None else self._get_energy_used()

            # Stop periodic Running attribute updates and pending finish timers so nothing repaints progress.
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None
            self._safe_cancel_timer(self.low_power_timer)
            self.low_power_timer = None
            self._safe_cancel_timer(self.pattern_check_timer)
            self.pattern_check_timer = None

            self.state = "Unemptied"
            effective_key = self._get_confirmed_programme_key() or self.detected_programme
            attributes = {
                **self._attrs_clear_running_progress(),
                "cycle_complete": True,
                "run_time_minutes": round(run_minutes, 1),
                "derived_programme_key": effective_key,
            }
            if energy_used > 0:
                attributes["energy_used"] = round(energy_used, 3)
            if self.keep_fresh_detected:
                attributes["keep_fresh_detected"] = True

            self._set_state_entity( state="Unemptied", attributes=attributes, replace=True)

            if self.poll_timer:
                self._safe_cancel_timer(self.poll_timer)
                self.poll_timer = None

            # Cancel running watchdog, start unemptied watchdog
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = self.run_in(
                self._unemptied_watchdog_timeout,
                int(self.unemptied_timeout_hours * 3600)
            )

            self.log(f"State -> Unemptied (ran {run_minutes:.1f} min)", level="INFO")

            # Send notification - dryer done, please empty!
            # Skip when skip_announce=True (user opened door before we detected finish).
            announce_enabled = True
            if self.announce_entity:
                try:
                    announce_enabled = self.get_state(self.announce_entity) == "on"
                except Exception:
                    pass
            if (not skip_announce and self.sonos_notifier and not self.notification_sent
                    and announce_enabled):
                try:
                    msg = self.announce_message if getattr(self, "announce_message", None) else "Dryer is ready to be emptied"
                    self.sonos_notifier.notify(message=msg)
                    self.log("Dryer announcement sent", level="INFO")
                    self.notification_sent = True
                except Exception as e:
                    self.log(f"Error sending notification: {e}", level="ERROR")

    def _transition_to_emptied(self, reason):
        """Transition to Emptied state (door open, user is emptying)."""
        if self._should_change_state("Emptied", force=True):  # Door event bypasses cooling
            energy_used = self._get_energy_used()
            run_minutes = self._get_run_duration_minutes()

            self.state = "Emptied"
            effective_key = self._get_confirmed_programme_key() or self.detected_programme
            attributes = {
                **self._attrs_clear_running_progress(),
                "reason": reason,
                "run_time_minutes": round(run_minutes, 1) if run_minutes > 0 else None,
                "derived_programme_key": effective_key,
            }
            if energy_used > 0:
                attributes["energy_used"] = round(energy_used, 3)
            if self.keep_fresh_detected:
                attributes["keep_fresh_detected"] = True

            self._set_state_entity( state="Emptied", attributes=attributes, replace=True)

            # Cancel unemptied watchdog since we're now emptying
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None

            # Re-enable announce toggle for next cycle (match washer)
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
        self.energy_start = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False
        self.keep_fresh_detected = False
        self.power_readings = []
        self.high_power_counter = 0

        if self.poll_timer:
            self._safe_cancel_timer(self.poll_timer)
            self.poll_timer = None

        if self.pause_timer:
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None

        if self.low_power_timer:
            self._safe_cancel_timer(self.low_power_timer)
            self.low_power_timer = None

        if self.pattern_check_timer:
            self._safe_cancel_timer(self.pattern_check_timer)
            self.pattern_check_timer = None
        if self.classify_timer:
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None

        self.expected_dur_at_start = None
        self.detected_programme = "unknown"
        self.max_power_w = 0.0

        # Cancel watchdog timers
        if self.running_watchdog_timer:
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
        if self.unemptied_watchdog_timer:
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None

        if self.start_confirm_timer:
            self._safe_cancel_timer(self.start_confirm_timer)
            self.start_confirm_timer = None
        self.door_fast_start_armed_until = None

    def _power_changed(self, entity, attr, old, new, kwargs):
        try:
            if new in ["unknown", "unavailable"]:
                self._handle_unavailable(entity, attr, old, new, kwargs)
                return
            watts = float(new or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(entity, attr, old, new, kwargs)
            return

        current_state = self.get_state(self.state_entity)

        # Record power readings when running
        if current_state == "Running":
            self._record_power_reading(watts)
            if watts > self.max_power_w:
                self.max_power_w = watts

        # Do NOT revert Unemptied -> Running when power rises again. After a real cycle ends, Miele
        # keep-fresh / anti-crease often draws >= start_w while the door is still closed; that is
        # still Unemptied (waiting to empty), not a "false" Unemptied. Leave Unemptied until door
        # or unemptied watchdog.

        # Track sustained high power
        if watts >= self.main_cycle_power:
            self.high_power_counter += 1
        else:
            self.high_power_counter = 0

        # High power branch (start detection) - single pending confirm timer (no stacking)
        if watts >= self.start_w:
            if current_state == "Off":
                if not self.start_confirm_timer:
                    now = self._now_utc()
                    armed = (
                        self.door_fast_start_armed_until is not None
                        and now <= self.door_fast_start_armed_until
                    )
                    delay = self.door_close_fast_confirm_s if armed else self.run_for
                    self.log(
                        f"Power >= {self.start_w}W -> scheduling start confirm in {delay}s "
                        f"({'fast-start window' if armed else 'normal'})",
                        level="DEBUG",
                    )
                    self.start_confirm_timer = self.run_in(self._confirm_running, delay)
        elif current_state == "Off":
            if self.start_confirm_timer:
                self._safe_cancel_timer(self.start_confirm_timer)
                self.start_confirm_timer = None

        # Low power branch (finish detection)
        elif current_state == "Running" and watts <= self.stop_w:
            if not self.low_power_timer:
                self.log(f"Power <= {self.stop_w}W -> may be finished", level="DEBUG")

                # Start pattern detection for keep-fresh/anti-crease
                if not self.pattern_check_timer and not self.keep_fresh_detected:
                    self.pattern_check_timer = self.run_every(
                        self._check_power_pattern,
                        self.datetime() + timedelta(seconds=self.power_reading_interval),
                        self.power_reading_interval,
                    )

                self.low_power_timer = self.run_in(self._confirm_finished, self.stop_for)

        # Recovered spike
        elif current_state == "Running" and watts > self.stop_w and self.low_power_timer:
            self._safe_cancel_timer(self.low_power_timer)
            self.low_power_timer = None
            self.log(f"Power recovered to {watts}W", level="DEBUG")

    def _check_power_pattern(self, kwargs):
        """Check power readings for anti-crease/keep-fresh pattern (Miele TCB150 WP feature)."""
        if len(self.power_readings) < self.pattern_window or self.state != "Running":
            return

        try:
            mean_power = statistics.mean(self.power_readings)
            max_power = max(self.power_readings)
            min_power = min(self.power_readings)
            stdev = statistics.stdev(self.power_readings) if len(self.power_readings) > 1 else 0

            # Typical keep-fresh/anti-crease pattern: intermittent tumbles with zero periods
            has_zero_periods = min_power < 5
            has_moderate_spikes = 100 < max_power < self.main_cycle_power
            not_main_cycle = mean_power < self.main_cycle_power / 3

            main_cycle_done = False

            if has_zero_periods and has_moderate_spikes and not_main_cycle:
                main_cycle_done = True

            if mean_power < 250 and max_power < self.main_cycle_power and stdev > 50:
                main_cycle_done = True

            if main_cycle_done:
                self.log("Anti-crease/keep-fresh mode detected - dryer main cycle complete", level="INFO")
                self.keep_fresh_detected = True
                run_min = self._get_run_duration_minutes()
                prog = self._classify_programme()
                effective_key = self._get_confirmed_programme_key() or prog
                guard_dur = self._get_guard_duration(tick_prog=effective_key)
                if run_min < guard_dur * 0.8:
                    self.log(f"Keep-fresh: run {run_min:.0f}min < 80% of {guard_dur:.0f}min - blocking Unemptied", level="DEBUG")
                elif not self._is_valid_completed_cycle():
                    self.log("Keep-fresh: cycle validation failed", level="DEBUG")
                else:
                    profile = self._get_profile(prog)
                    if profile.get("is_real", True) is False:
                        self.log("Keep-fresh: non-real programme (e.g. finish_uld) - not declaring Unemptied", level="DEBUG")
                    else:
                        run_minutes_wall = run_min
                        run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Keep-fresh ")
                        idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                        energy_kwh = self._get_energy_used()
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
                            end_reason="keep_fresh_detected",
                            idle_min=idle_min,
                        )
                if self.pattern_check_timer:
                    self._safe_cancel_timer(self.pattern_check_timer)
                    self.pattern_check_timer = None

        except Exception as e:
            self.log(f"Error in pattern analysis: {e}", level="WARNING")

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

    def _confirm_running(self, kwargs):
        self.start_confirm_timer = None

        power_state = self.get_state(self.power_sensor)
        if power_state in ["unknown", "unavailable", None]:
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        try:
            watts_confirm = float(power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        if watts_confirm < self.start_w:
            self.log(
                f"Start confirm skipped: live power {watts_confirm:.1f}W < {self.start_w}W (stale/noise) - "
                f"fast-start arm preserved if still in window",
                level="DEBUG",
            )
            return

        if watts_confirm >= self.start_w:
            if self._should_change_state("Running"):
                self.state = "Running"
                self.start_time = self._now_utc()
                self.keep_fresh_detected = False
                self.power_readings = []
                self.notification_sent = False
                self.door_opened_during_cycle = False
                self.detected_programme = "unknown"
                self.max_power_w = watts_confirm

                try:
                    energy = self.get_state(self.energy_sensor)
                    if energy is not None and energy not in ["unknown", "unavailable"]:
                        self.energy_start = float(energy)
                except (ValueError, TypeError):
                    self.energy_start = None

                prog = self._classify_programme()
                self.detected_programme = prog
                effective_key = self._get_confirmed_programme_key() or prog
                self.expected_dur_at_start = self._get_guard_duration(tick_prog=effective_key)
                self._set_state_entity( state="Running")
                self._update_running_attributes()

                if not self.poll_timer:
                    self.poll_timer = self.run_in(self._poll_power, 60)
                if not self.classify_timer:
                    self.classify_timer = self.run_in(self._tick_classify, 30)

                # Start running watchdog
                self._safe_cancel_timer(self.running_watchdog_timer)
                self.running_watchdog_timer = self.run_in(
                    self._running_watchdog_timeout,
                    int(self.max_running_hours * 3600)
                )

                self.log("State -> Running", level="INFO")
                self.door_fast_start_armed_until = None

    def _confirm_finished(self, kwargs):
        """Confirm the cycle has finished - power dropped, door still closed. 80% guard."""
        current_state = self.get_state(self.state_entity)
        if current_state != "Running":
            self.low_power_timer = None
            return

        power_state = self.get_state(self.power_sensor)
        if power_state in ["unknown", "unavailable", None]:
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        try:
            watts = float(power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        if self.keep_fresh_detected or watts <= self.stop_w:
            run_min = self._get_run_duration_minutes()
            prog = self._classify_programme()
            effective_key = self._get_confirmed_programme_key() or prog
            guard_dur = self._get_guard_duration(tick_prog=effective_key)
            if run_min < guard_dur * 0.8:
                self.log(f"Power-based: run {run_min:.0f}min < 80% of {guard_dur:.0f}min - blocking", level="DEBUG")
                self.low_power_timer = None
                return
            if self._is_valid_completed_cycle():
                profile = self._get_profile(prog)
                skip_announce = profile.get("is_real", True) is False  # e.g. finish_uld
                run_minutes_wall = run_min
                run_minutes, duration_source = self._correct_duration(run_minutes_wall)
                idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                energy_kwh = self._get_energy_used()
                self._transition_to_unemptied(skip_announce=skip_announce, run_minutes=run_minutes, energy_used=energy_kwh)
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
                if self.pattern_check_timer:
                    self._safe_cancel_timer(self.pattern_check_timer)
                    self.pattern_check_timer = None
            else:
                self.log("Cycle incomplete - waiting", level="DEBUG")

        self.low_power_timer = None

    def _handle_unavailable(self, entity, attribute, old, new, kwargs):
        """Handle entity becoming unavailable"""
        self.log(f"{entity} became unavailable ({new}), setting state to Off", level="WARNING")
        self.state = "Off"
        self.last_state_change = datetime.now()
        self._set_state_entity( state="Off")
        self._reset_programme_selectors_to_unconfirmed()
        self._reset_cycle_tracking()
