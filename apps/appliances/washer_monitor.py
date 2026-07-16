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
import copy
import time
import os
import yaml
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore


def _parse_utc(s: str):
    """Parse ISO timestamp to timezone-aware UTC datetime. Handles 'Z' and no suffix."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    if s.endswith("+00:00") or s.endswith("Z"):
        pass
    elif "+" not in s[-7:] and "-" not in s[-7:]:
        s = s + "+00:00"  # assume UTC if no offset
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


class WasherMonitor(hass.Hass):
    # Programme profiles loaded from washer_programmes.yaml at startup.
    # Programme and temperature are independent dimensions.  For "bomuld",
    # the profile depends on the selected temperature (by_temperature dict).
    # UI matrix: allowed_temperatures, allowed_spin_speeds, default_temperature, default_spin, available_options (canonical values only).
    _PROGRAMME_DISPLAY_ORDER = [
        "ekspres", "uld", "bomuld", "finvask", "strygelet", "eco",
        "morkt_denim", "outdoor", "impraegnering", "pumpe_centrifugering", "kun_skyl_stivelse",
    ]
    _CANONICAL_SPIN = ["1400 rpm", "1200 rpm", "900 rpm", "700 rpm", "No spin"]
    _SPIN_MAX_1200 = ["1200 rpm", "900 rpm", "700 rpm", "No spin"]   # Manual: Strygelet, Uld, Ekspres, Mørkt/Denim, Imprægnering
    _SPIN_MAX_900 = ["900 rpm", "700 rpm", "No spin"]                 # Manual: Finvask, Outdoor
    _DEFAULT_PROFILES = {
        "ekspres":   {"label": "Ekspres",   "default_temp": None,   "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": _SPIN_MAX_1200, "default_temperature": "40°C", "default_spin": "1200 rpm", "available_options": ["short"], "by_temperature": {
            "cold": {"duration_min": 20, "max_energy_kwh": 0.25, "heats": False, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
            "20°C": {"duration_min": 20, "max_energy_kwh": 0.40, "heats":  True, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
            "30°C": {"duration_min": 20, "max_energy_kwh": 0.40, "heats":  True, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
            "40°C": {"duration_min": 20, "max_energy_kwh": 0.40, "heats":  True, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
        }},
        "uld":       {"label": "Uld",       "default_temp": "30°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": _SPIN_MAX_1200, "default_temperature": "30°C", "default_spin": "1200 rpm", "available_options": [], "by_temperature": {
            "cold": {"duration_min":  39, "max_energy_kwh": 0.22, "heats": False, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
            "20°C": {"duration_min":  39, "max_energy_kwh": 0.26, "heats":  True, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
            "30°C": {"duration_min":  39, "max_energy_kwh": 0.28, "heats":  True, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
            "40°C": {"duration_min":  39, "max_energy_kwh": 0.28, "heats":  True, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
        }},  # Manual: Uld 40°C -> cold; max spin 1200 rpm
        "bomuld":    {"label": "Bomuld",    "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C", "90°C"], "allowed_spin_speeds": _CANONICAL_SPIN, "default_temperature": "40°C", "default_spin": "1400 rpm", "available_options": ["water_plus", "soak", "prewash"], "by_temperature": {
            "cold": {"duration_min": 159, "max_energy_kwh": 0.35, "heats": False, "stable_min": 10, "max_dur_min": 185, "supports_anti_crease": True},
            "20°C": {"duration_min": 159, "max_energy_kwh": 0.55, "heats":  True, "stable_min": 15, "max_dur_min": 185, "supports_anti_crease": True},
            "30°C": {"duration_min": 159, "max_energy_kwh": 0.75, "heats":  True, "stable_min": 15, "max_dur_min": 185, "supports_anti_crease": True},
            "40°C": {"duration_min": 175, "max_energy_kwh": 0.90, "heats":  True, "stable_min": 15, "max_dur_min": 210, "supports_anti_crease": True},
            "60°C": {"duration_min": 149, "max_energy_kwh": 1.40, "heats":  True, "stable_min": 15, "max_dur_min": 175, "supports_anti_crease": True},
            "90°C": {"duration_min": 160, "max_energy_kwh": 2.20, "heats":  True, "stable_min": 15, "max_dur_min": 195, "supports_anti_crease": True},
        }},
        "finvask":   {"label": "Finvask",   "default_temp": "40°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": _SPIN_MAX_900, "default_temperature": "40°C", "default_spin": "900 rpm", "available_options": ["water_plus", "soak"], "by_temperature": {
            "cold": {"duration_min":  65, "max_energy_kwh": 0.25, "heats": False, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
            "20°C": {"duration_min":  65, "max_energy_kwh": 0.35, "heats":  True, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
            "30°C": {"duration_min":  65, "max_energy_kwh": 0.38, "heats":  True, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
            "40°C": {"duration_min":  65, "max_energy_kwh": 0.40, "heats":  True, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
        }},  # Manual: 40°C -> cold; allows all lower temps (Cold, 20, 30, 40)
        "strygelet": {"label": "Strygelet", "default_temp": "30°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C"], "allowed_spin_speeds": _SPIN_MAX_1200, "default_temperature": "30°C", "default_spin": "1200 rpm", "available_options": ["water_plus", "soak"], "by_temperature": {
            "cold": {"duration_min": 119, "max_energy_kwh": 0.35, "heats": False, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
            "20°C": {"duration_min": 119, "max_energy_kwh": 0.45, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
            "30°C": {"duration_min": 119, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
            "40°C": {"duration_min": 119, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
            "60°C": {"duration_min": 119, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
        }},  # Manual: 60°C -> cold; intermediate temps 20–40
        "eco":       {"label": "ECO",       "default_temp": "40-60°C", "allowed_temperatures": ["40-60°C", "40°C", "60°C"], "default_temperature": "40-60°C", "allowed_spin_speeds": _CANONICAL_SPIN, "default_spin": "1400 rpm", "available_options": ["water_plus", "soak"], "duration_min": 199, "max_energy_kwh": 0.78, "heats":  True, "stable_min": 15, "max_dur_min": 235, "supports_anti_crease": True},  # Manual p62 at 7 kg: 3:19 (199 min); full load typical
        "morkt_denim":  {"label": "Mørkt/Denim", "default_temp": "60°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C"], "allowed_spin_speeds": _SPIN_MAX_1200, "default_temperature": "60°C", "default_spin": "1200 rpm", "available_options": ["water_plus"], "by_temperature": {
            "cold": {"duration_min":  90, "max_energy_kwh": 0.35, "heats": False, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
            "20°C": {"duration_min":  90, "max_energy_kwh": 0.42, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
            "30°C": {"duration_min":  90, "max_energy_kwh": 0.48, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
            "40°C": {"duration_min":  90, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
            "60°C": {"duration_min":  90, "max_energy_kwh": 0.55, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
        }},  # Manual: 60°C -> cold; intermediate temps 20–40
        "outdoor":   {"label": "Outdoor",   "default_temp": "40°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": _SPIN_MAX_900, "default_temperature": "40°C", "default_spin": "900 rpm", "available_options": ["water_plus"], "by_temperature": {
            "cold": {"duration_min":  50, "max_energy_kwh": 0.25, "heats": False, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
            "20°C": {"duration_min":  50, "max_energy_kwh": 0.32, "heats":  True, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
            "30°C": {"duration_min":  50, "max_energy_kwh": 0.36, "heats":  True, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
            "40°C": {"duration_min":  50, "max_energy_kwh": 0.40, "heats":  True, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
        }},  # Manual: 40°C -> cold; max spin 900 rpm
        "impraegnering": {"label": "Imprægnering", "default_temp": None, "allowed_temperatures": [], "default_temperature": None, "allowed_spin_speeds": _SPIN_MAX_1200, "default_spin": "1200 rpm", "available_options": [], "duration_min":  25, "max_energy_kwh": 0.15, "heats": False, "stable_min":  8, "max_dur_min":  35, "supports_anti_crease": True},
        "pumpe_centrifugering": {"label": "Pumpe/Centrifugering", "default_temp": None, "allowed_temperatures": [], "default_temperature": None, "allowed_spin_speeds": _CANONICAL_SPIN, "default_spin": "1400 rpm", "available_options": [], "duration_min":  10, "max_energy_kwh": 0.08, "heats": False, "stable_min":  5, "max_dur_min":  20, "supports_anti_crease": False},
        "kun_skyl_stivelse": {"label": "Kun skyl/stivelse", "default_temp": None, "allowed_temperatures": [], "default_temperature": None, "allowed_spin_speeds": _CANONICAL_SPIN, "default_spin": "1400 rpm", "available_options": [], "duration_min":  30, "max_energy_kwh": 0.12, "heats": False, "stable_min":  8, "max_dur_min":  45, "supports_anti_crease": True},
        "unknown":   {"label": "Unknown",   "default_temp": None,   "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C", "90°C"], "allowed_spin_speeds": _CANONICAL_SPIN, "default_temperature": None, "default_spin": "1400 rpm", "available_options": [], "duration_min": 180, "max_energy_kwh": 2.50, "heats":  None, "stable_min": 15, "max_dur_min": 240, "supports_anti_crease": True},
    }
    PROGRAMME_PROFILES = dict(_DEFAULT_PROFILES)


    def _get_profile(self, programme: str, temperature=None):
        """Return the flat profile dict for a programme + optional temperature.

        For 'bomuld' with by_temperature, resolves to the matching sub-profile.
        Falls back to the first sub-profile if temperature is missing/unknown.
        For other programmes, returns the top-level profile directly.
        """
        prof = self.PROGRAMME_PROFILES.get(programme, self.PROGRAMME_PROFILES.get("unknown", {}))
        if "by_temperature" in prof:
            temps = prof["by_temperature"]
            if temperature and temperature in temps:
                p = dict(temps[temperature])
                p["label"] = prof.get("label", programme)
                return p
            first = next(iter(temps.values()))
            p = dict(first)
            p["label"] = prof.get("label", programme)
            return p
        return prof

    def _programme_has_temperature(self, programme: str) -> bool:
        """Return True if this programme has temperature-dependent profiles (e.g. bomuld).
        Only then do we persist/learn temperature; otherwise learn_key is just programme."""
        prof = self.PROGRAMME_PROFILES.get(programme, self.PROGRAMME_PROFILES.get("unknown", {}))
        return isinstance(prof, dict) and "by_temperature" in prof

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
                merged = copy.deepcopy(self._DEFAULT_PROFILES)
                for key, val in profiles.items():
                    if not isinstance(val, dict):
                        merged[key] = val
                        continue
                    if key in merged and "by_temperature" in merged.get(key, {}):
                        # Merge by_temperature so YAML can override a subset of temps
                        base = merged[key]
                        for t, p in val.get("by_temperature", {}).items():
                            base["by_temperature"][t] = {**base["by_temperature"].get(t, {}), **p}
                        for k, v in val.items():
                            if k != "by_temperature":
                                base[k] = v
                    else:
                        merged[key] = {**merged.get(key, {}), **val}
                # ECO: ensure temperature options are always 40-60, 40, 60
                if "eco" in merged and isinstance(merged["eco"], dict):
                    eco_at = merged["eco"].get("allowed_temperatures") or []
                    if set(eco_at) != {"40-60°C", "40°C", "60°C"}:
                        merged["eco"]["allowed_temperatures"] = ["40-60°C", "40°C", "60°C"]
                        if not merged["eco"].get("default_temperature"):
                            merged["eco"]["default_temperature"] = "40-60°C"
                if "unknown" not in merged:
                    merged["unknown"] = copy.deepcopy(self._DEFAULT_PROFILES["unknown"])
                WasherMonitor.PROGRAMME_PROFILES = merged
                # Build label -> key from profiles and stable display order (so new YAML programmes appear)
                label_to_key = {}
                for key in self._programme_display_order:
                    if key in merged:
                        label = merged[key].get("label", key)
                        label_to_key[label] = key
                if "unknown" in merged:
                    label_to_key[merged["unknown"].get("label", "Unknown")] = "unknown"
                # Legacy options with temperature suffix (backwards compatibility)
                for leg_label, leg_key in [
                    ("Ekspres 20", "ekspres"), ("Uld 30", "uld"), ("Bomuld 20", "bomuld"), ("Bomuld 60", "bomuld"),
                    ("Finvask 40", "finvask"), ("Strygelet 30", "strygelet"), ("ECO 40-60", "eco"),
                ]:
                    label_to_key[leg_label] = leg_key
                WasherMonitor._LABEL_TO_KEY = label_to_key
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

    @staticmethod
    def _log_safe(s):
        """Return string safe for log output (avoids encoding errors with ° in some environments)."""
        if s is None:
            return ""
        return str(s).replace("\u00b0", " ").replace("\u00c2\u00b0", " ")

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

    def _set_state_entity(self, **kwargs):
        """Publish to sensor.*_state and sync input_select.* when state changes."""
        self.set_state(self.state_entity, **kwargs)
        st = kwargs.get("state")
        if st is not None:
            self._sync_ui_select(st)

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
        self.start_w = float(self.args["start_w"])
        self.stop_w = float(self.args["stop_w"])
        self.run_for = int(self.args.get("run_for", 60))
        self.programs = self.args.get("programs", {})

        # Cycle validation thresholds
        self.min_cycle_minutes = int(self.args.get("min_cycle_minutes", 25))
        self.min_energy_kwh = float(self.args.get("min_energy_kwh", 0.2))
        self.completion_guard_fraction = float(self.args.get("completion_guard_fraction", 0.65))
        self.completion_guard_fraction_user_confirmed = float(self.args.get("completion_guard_fraction_user_confirmed", 0.60))
        self.pause_timeout_minutes = int(self.args.get("pause_timeout_minutes", 10))
        self.addload_window_minutes = int(self.args.get("addload_window_minutes", 5))  # AddLoad only at start
        self.pause_window_minutes = int(self.args.get("pause_window_minutes", 3))  # Paused only relevant in first 3 min
        self.restore_start_gap_minutes = int(self.args.get("restore_start_gap_minutes", 15))  # min continuous low power after restored start to treat as stale and re-infer from history

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
        self.energy_start = None
        self.poll_timer = None
        self.history_poll_timer = None  # Periodic power-history check to catch missed heating
        self.last_state_change = None
        self.last_door_closed_at = None  # Last time door was closed (start time cannot be before this, except AddLoad)
        self.last_door_closed_trusted = False  # True only after a real door close (not stale entity / infer-only)
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
        
        # Finish confirmation flag
        self.finish_confirmed = False
        self._zero_power_since = None  # Standby backstop: when power first dropped to 0W
        # Pending end reason when transitioning from anti-crease path (so _transition_to_unemptied can store it in feedback).
        self._pending_end_reason = None  # e.g. "anti_crease_pattern"
        self._pending_tail_mean_w = None
        self._pending_tail_std_w = None
        self._pending_tail_peak_w = None

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
        self.expected_dur_at_start: float | None = None  # Frozen when programme first classified; used in guards
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
        self._last_infer_start_attempt = None  # Throttle _infer_start_from_state_history
        self._last_finish_guard_info_log_at = None  # Throttle repetitive INFO finish-guard lines

        # Notification
        self.announce_message = self.args.get("announce_message", "Washer is ready to be emptied")
        self.announce_entity = self.args.get("announce_entity")  # input_boolean to enable/disable
        # Optional: announce when door *unlocks* instead of when we enter Unemptied.
        self.door_lock_entity = self.args.get("door_lock_entity")  # e.g. lock.washer_door

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

        # Restore previous state
        existing = self.get_state(self.state_entity)
        valid_states = ("Running", "Unemptied", "Paused", "Emptied")
        self.state = existing if existing in valid_states else "Off"
        recovery_off_to_running = False
        # HA restart (or recorder glitch) can leave sensor Off while the washer is actually drawing start power.
        # If we skip restore and bootstrap calls _confirm_running, we wipe start_time and user context.
        if self.state == "Off":
            try:
                boot_watts = float(self.get_state(self.power_sensor) or 0)
            except (ValueError, TypeError):
                boot_watts = 0.0
            if boot_watts >= self.start_w:
                self.log(
                    f"Initialize: {self.state_entity} is Off but power is {boot_watts:.0f}W "
                    f"(>= {self.start_w:.0f}W) - treating as restart during an active wash; forcing Running",
                    level="WARNING",
                )
                self.state = "Running"
                recovery_off_to_running = True

        # Only publish when state differs. Re-sending the same state can strip attributes on some HA/AppDaemon setups.
        if self.state != existing:
            if recovery_off_to_running:
                try:
                    full = self.get_state(self.state_entity, attribute="all") or {}
                    keep = dict((full.get("attributes") or {}))
                    self._set_state_entity( state="Running", attributes=keep, replace=True)
                except Exception:
                    self._set_state_entity( state=self.state)
            else:
                self._set_state_entity( state=self.state)

        # If we were Running before restart, restore in-memory state from persisted attributes
        # so ETA, energy-used, and "user confirmed programme" survive app reloads.
        if self.state == "Running":
            self._restore_running_state()
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

    def _restore_running_state(self):
        """Restore in-memory state when we were Running before an app restart.

        Reads cycle_start_time, energy_at_start, detected_programme from the
        state entity attributes (we persist these while Running). Also sets
        programme_confirmed_by_user if the user had selected a programme in the
        confirm dropdown. Restarts energy-check and watchdog timers so finish
        detection continues.
        """
        try:
            start_str = self.get_state(self.state_entity, attribute="cycle_start_time")
            if start_str:
                self.start_time = _parse_utc(start_str)
                if self.start_time:
                    self.log(f"Restored cycle start time: {start_str}", level="DEBUG")
        except (TypeError, ValueError, AttributeError) as e:
            self.log(f"Could not restore cycle_start_time: {e}", level="DEBUG")

        # last_door_closed_at on the entity may be from before we stored trust, or wrong; only use when trusted.
        self.last_door_closed_trusted = False
        try:
            trusted_attr = self.get_state(self.state_entity, attribute="last_door_closed_trusted")
            trusted = self._attr_bool_true(trusted_attr)
            last_door_str_entity = self.get_state(self.state_entity, attribute="last_door_closed_at")
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
            last_off_str = self.get_state(self.state_entity, attribute="last_off_at")
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
            if self.start_time and not self.last_door_closed_trusted:
                full = self.get_state(self.state_entity, attribute="all")
                if full and isinstance(full, dict):
                    last_changed_str = full.get("last_changed") or full.get("last_updated")
                    if last_changed_str:
                        last_changed_dt = _parse_utc(str(last_changed_str))
                        if last_changed_dt and self.start_time < last_changed_dt:
                            gap_seconds = (last_changed_dt - self.start_time).total_seconds()
                            if gap_seconds >= self.pause_window_minutes * 60:
                                self.log(
                                    f"Restore: correcting start_time to entity last_changed (cycle 2) "
                                    f"(was {self._strftime_local(self.start_time)}, now {self._strftime_local(last_changed_dt)})",
                                    level="INFO",
                                )
                                self.start_time = last_changed_dt
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
                        self._push_corrected_start_time_to_entity()
                        if self.use_energy_detection:
                            self._restore_energy_state_from_history()
            except Exception as e:
                self.log(f"Could not validate/correct start_time from power history: {e}", level="DEBUG")

        try:
            energy_at_start = self.get_state(self.state_entity, attribute="energy_at_start")
            if energy_at_start is not None:
                self.energy_start = float(energy_at_start)
                self.log(f"Restored energy_at_start: {self.energy_start}", level="DEBUG")
        except (TypeError, ValueError, AttributeError):
            pass

        try:
            prog = self.get_state(self.state_entity, attribute="detected_programme")
            if prog and prog != "unknown":
                restored_temp = self.get_state(self.state_entity, attribute="detected_temperature")
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
            hb = self.get_state(self.state_entity, attribute="heating_bursts")
            if (hb is None or int(hb or 0) == 0) and not self.observed_heating:
                self._restore_heating_from_power_history()
        except (TypeError, ValueError, AttributeError):
            pass

        # Restore expected_dur_at_start from selector only when user had confirmed (entity attribute).
        # Otherwise the selector may hold an auto-filled prediction from a previous run.
        try:
            confirmed_attr = self.get_state(self.state_entity, attribute="programme_confirmed_by_user")
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
                            self.log(f"Restored expected_dur_at_start from selector: {dur:.0f} min", level="DEBUG")
            except Exception:
                pass
        if self.expected_dur_at_start is None:
            try:
                v = self.get_state(self.state_entity, attribute="expected_dur_at_start")
                if v not in (None, "", "unknown", "unavailable"):
                    self.expected_dur_at_start = float(v)
                    self.log(f"Restored expected_dur_at_start from entity: {self.expected_dur_at_start:.0f} min", level="DEBUG")
            except (TypeError, ValueError, AttributeError):
                pass

        # Correct start_time if it's after last_door_closed_at (e.g. wrong from bad recovery).
        if (self.start_time and self.last_door_closed_trusted and self.last_door_closed_at and
                self.start_time > self.last_door_closed_at):
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
                confirmed_attr = self.get_state(self.state_entity, attribute="programme_confirmed_by_user")
                if confirmed_attr is True or confirmed_attr == "true" or confirmed_attr == "True":
                    self.programme_confirmed_by_user = True
                    by_attr = self.get_state(self.state_entity, attribute="programme_confirmed_by") or ""
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
            last_high_str = self.get_state(self.state_entity, attribute="last_high_energy_at")
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
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(points) < 2:
                return None
            points.sort(key=lambda x: x[0])
            for i, (t0, w0) in enumerate(points):
                if w0 < self.start_w:
                    continue
                highs = 1
                for j in range(i + 1, len(points)):
                    t1, w1 = points[j]
                    if (t1 - t0).total_seconds() > 25 * 60:
                        break
                    if w1 >= self.start_w:
                        highs += 1
                        if highs >= 2:
                            return t0
            return None
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
            self._set_state_entity( state="Running", attributes=attrs, replace=True)
        except Exception as e:
            self.log(f"Recovery sync programme from selector failed: {e}", level="DEBUG")

    def _flatten_history(self, hist, entity_id=None):
        """AppDaemon get_history returns list[list[dict]] (or occasionally dict). Normalize to list[dict]."""
        if isinstance(hist, dict):
            if entity_id is not None:
                hist = hist.get(entity_id, []) or hist.get("history", [])
            else:
                hist = next(iter(hist.values()), [])
        if isinstance(hist, list):
            if hist and isinstance(hist[0], list):
                return hist[0]
            return hist
        return []

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
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(points) < 3:
                return None
            points.sort(key=lambda x: x[0])
            gap_min_seconds = self.restore_start_gap_minutes * 60
            # Find first gap after from_time: continuous period >= gap_min_seconds with all power < start_w
            gap_start = None
            for i, (t, power) in enumerate(points):
                if t < from_time:
                    continue
                if power < self.start_w:
                    if gap_start is None:
                        gap_start = t
                else:
                    if gap_start is not None:
                        gap_len = (t - gap_start).total_seconds()
                        if gap_len >= gap_min_seconds:
                            # Found a long gap; first sustained high power after this is at t (current point)
                            # Require at least one more high reading soon after to be sure it's sustained
                            sustained_count = 1
                            for j in range(i + 1, len(points)):
                                if (points[j][0] - t).total_seconds() > 300:
                                    break
                                if points[j][1] >= self.start_w:
                                    sustained_count += 1
                                    if sustained_count >= 2:
                                        return t
                            if sustained_count >= 1:
                                return t
                        gap_start = None
            # Check if gap runs to end of window (still in gap at to_time)
            if gap_start is not None and (to_time - gap_start).total_seconds() >= gap_min_seconds:
                # Gap extends to now; no "after gap" high power in window - don't infer
                return None
            return None
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
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(points) < 2:
                return False
            points.sort(key=lambda x: x[0])
            bursts = 0
            in_burst = False
            max_w = self.max_power_seen
            for t, w in points:
                if w > max_w:
                    max_w = w
                if w > 1000:
                    if not in_burst:
                        in_burst = True
                        bursts += 1
                elif w < 500:
                    in_burst = False
            if bursts > 0 or max_w > 1000:
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
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(points) < 2:
                return (None, None)
            points.sort(key=lambda x: x[0])
            bursts = 0
            in_burst = False
            max_w = 0.0
            for _t, w in points:
                if w > max_w:
                    max_w = w
                if w > 1000:
                    if not in_burst:
                        in_burst = True
                        bursts += 1
                elif w < 500:
                    in_burst = False
            return (bursts, max_w)
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
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(points) < 2:
                return False
            points.sort(key=lambda x: x[0])
            cutoff = end_time - timedelta(minutes=20)
            self.energy_buffer = [(t, e) for t, e in points if t >= cutoff]
            self.last_energy_value = points[-1][1]
            self.last_energy_time = points[-1][0]
            # Last time we saw implied watts above threshold (cycle was still consuming)
            self.last_high_energy_at = None
            for i in range(1, len(points)):
                t1, e1 = points[i - 1]
                t2, e2 = points[i]
                delta_s = (t2 - t1).total_seconds()
                if delta_s < 10:
                    continue
                implied_w = ((e2 - e1) * 1000) / (delta_s / 3600)
                if implied_w > self.energy_active_watts:
                    self.last_high_energy_at = t2
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
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(points) < 2:
                self.log("Cycle end from history: not enough valid energy points", level="DEBUG")
                return None
            points.sort(key=lambda x: x[0])
            run_minutes_max = (end_time - self.start_time).total_seconds() / 60

            # Collect all timestamps that are "end of a high-power period" (implied watts above threshold).
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
                self.log("Cycle end from history: no high-power end candidates in energy series", level="DEBUG")
                return None

            if expected_duration_min is not None and expected_duration_min > 0:
                # User-confirmed (or hinted) programme: pick the high-end that gives duration
                # closest to expected, within valid range. This avoids picking a late spike or
                # early heating end when the real cycle end is around expected_duration.
                best_end = None
                best_diff = float("inf")
                for t in high_end_candidates:
                    dur = (t - self.start_time).total_seconds() / 60
                    if dur < self.min_cycle_minutes or dur > run_minutes_max:
                        continue
                    diff = abs(dur - expected_duration_min)
                    if diff < best_diff:
                        best_diff = diff
                        best_end = t
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
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(points) < 6:
                return False
            points.sort(key=lambda x: x[0])
            watts = [w for _, w in points]
            mean_w = sum(watts) / len(watts)
            variance = sum((w - mean_w) ** 2 for w in watts) / len(watts)
            std_w = variance ** 0.5
            if (self.post_cycle_pattern_mean_low <= mean_w <= self.post_cycle_pattern_mean_high and
                    std_w >= self.post_cycle_pattern_min_std):
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
            points = []
            for entry in hist:
                try:
                    ts_str = entry.get("last_changed") or entry.get("last_updated")
                    if not ts_str:
                        continue
                    t = _parse_utc(ts_str)
                    if t is None:
                        continue
                    s = entry.get("state")
                    if s is None or s in ("unknown", "unavailable", ""):
                        continue
                    points.append((t, float(s)))
                except (ValueError, TypeError, AttributeError):
                    continue
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
        now = self._now_utc()
        total_seconds = 0.0
        weighted_sum = 0.0
        weighted_sq_sum = 0.0
        duty_above = 0.0
        peak_w = 0.0
        active_w = self.energy_active_watts
        for i, (t, w) in enumerate(points):
            if w > peak_w:
                peak_w = w
            if i + 1 < len(points):
                dt = (points[i + 1][0] - t).total_seconds()
            else:
                dt = (now - t).total_seconds()
            if dt <= 0:
                continue
            total_seconds += dt
            weighted_sum += w * dt
            weighted_sq_sum += w * w * dt
            if w > active_w:
                duty_above += dt
        if total_seconds <= 0:
            return (None, None, None, None)
        mean_w = weighted_sum / total_seconds
        variance = (weighted_sq_sum / total_seconds) - (mean_w * mean_w)
        std_w = (variance ** 0.5) if variance > 0 else 0.0
        duty_above_frac = duty_above / total_seconds
        return (mean_w, std_w, peak_w, duty_above_frac)

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
        if not points:
            return None
        last_t = None
        for t, w in points:
            if w > thr:
                last_t = t
        return last_t

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
        watts = [w for t, w in points if t >= cutoff]
        if len(watts) < 3:
            watts = [w for _, w in points]
        if not watts:
            return False
        peak_w = max(watts)
        mean_w = sum(watts) / len(watts)
        return (
            peak_w <= self.tail_idle_peak_max_watts
            and mean_w <= self.post_cycle_idle_watts
        )

    def _extract_tail_pulse_times(self, points):
        """Extract pulse timestamps from power points using an edge detector with gap de-duplication."""
        if not points:
            return []
        pulse_times = []
        in_pulse = False
        thr = self.tail_pattern_pulse_threshold_watts
        for t, w in points:
            if w >= thr:
                if not in_pulse:
                    if not pulse_times or (t - pulse_times[-1]).total_seconds() >= self.tail_pattern_min_gap_seconds:
                        pulse_times.append(t)
                    in_pulse = True
            elif w <= max(0.0, thr * 0.5):
                in_pulse = False
        return pulse_times

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
        if len(pulse_times) < self.tail_pattern_lock_min_pulses:
            return
        gaps = []
        for i in range(1, len(pulse_times)):
            gap_s = (pulse_times[i] - pulse_times[i - 1]).total_seconds()
            if self.tail_pattern_min_gap_seconds <= gap_s <= self.tail_pattern_max_gap_seconds:
                gaps.append(gap_s)
        if len(gaps) < max(3, self.tail_pattern_lock_min_pulses - 1):
            return
        gaps_sorted = sorted(gaps)
        med_gap = gaps_sorted[len(gaps_sorted) // 2]
        if med_gap <= 0:
            return
        max_dev = max(abs(g - med_gap) for g in gaps)
        if (max_dev / med_gap) > self.tail_pattern_max_jitter_fraction:
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
            if not points:
                return False
            active_w = self.energy_active_watts
            above_active = sum(1 for _, p in points if p > active_w)
            if above_active / max(1, len(points)) > self.anti_crease_max_duty_above_active:
                return True
            if any(p > 500 for _, p in points):
                return True
            return False
        if duty_above > self.anti_crease_max_duty_above_active:
            return True
        if peak_w > 500:
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
            ok = (
                mean_w <= self.anti_crease_tail_max_mean_w
                and std_w >= self.anti_crease_tail_min_std_w
                and duty_above <= self.anti_crease_max_duty_above_active
                and peak_w <= 500  # No heating burst in window
            )
            if ok and self.anti_crease_tail_max_peak_w is not None:
                ok = peak_w <= self.anti_crease_tail_max_peak_w
            return (ok, mean_w, std_w, peak_w)
        points = self._get_recent_power_history(self.anti_crease_window_minutes)
        if len(points) < 5:
            return (False, None, None, None)
        watts = [p for _, p in points]
        import statistics
        mean_w = statistics.mean(watts)
        try:
            std_w = statistics.stdev(watts)
        except statistics.StatisticsError:
            std_w = 0.0
        peak_w = max(watts)
        active_w = self.energy_active_watts
        duty_above = sum(1 for w in watts if w > active_w) / len(watts)
        ok = (
            mean_w <= self.anti_crease_tail_max_mean_w
            and std_w >= self.anti_crease_tail_min_std_w
            and duty_above <= self.anti_crease_max_duty_above_active
            and not any(w > 500 for w in watts)
        )
        if ok and self.anti_crease_tail_max_peak_w is not None:
            ok = peak_w <= self.anti_crease_tail_max_peak_w
        return (ok, mean_w, std_w, peak_w)

    def _power_looks_like_cycle_end(self, window_minutes: float | None = None) -> tuple[bool, float | None, float | None]:
        """True only if recent power pattern looks like real cycle end (anti-crease or machine off), not mid-cycle rinse.
        Mid-cycle rinse: mean ~50–80W, peaks 150–250W. Real end: mean ~18–45W, peak <120W, or flat idle (mean <12W).
        Returns (ok, mean_w, peak_w) for logging."""
        w = window_minutes or self.anti_crease_window_minutes
        points = self._get_recent_power_history(w)
        if len(points) < 5:
            return (False, None, None)
        watts = [p for _, p in points]
        import statistics
        mean_w = statistics.mean(watts)
        peak_w = max(watts)
        # Real anti-crease or gentle tail: low mean, no high spikes (we saw mean ~18–20W, peak 47W).
        if mean_w <= self.finish_power_gate_max_mean_w and peak_w <= self.finish_power_gate_max_peak_w:
            return (True, mean_w, peak_w)
        # Machine fully off: flat idle (manual: 0–2.8W; allow a bit of sensor noise).
        if mean_w <= self.finish_power_gate_off_max_mean_w and peak_w <= self.finish_power_gate_off_max_peak_w:
            return (True, mean_w, peak_w)
        return (False, mean_w, peak_w)

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
        Finish detection decides UI state; validation only classifies the saved record."""
        flags = []
        profile = self._get_profile(confirmed, confirmed_temperature)
        nominal_dur = profile.get("duration_min") or profile.get("nominal_duration_min") or 180
        max_dur = profile.get("max_dur_min") or profile.get("max_valid_duration_min") or int(nominal_dur * 1.2)
        user_conf = user_confirmed_override if user_confirmed_override is not None else self.programme_confirmed_by_user
        frac = self.completion_guard_fraction_user_confirmed if user_conf else self.completion_guard_fraction
        min_valid_dur = max(frac * nominal_dur, self.min_cycle_minutes)
        min_energy = profile.get("min_valid_energy_kwh") or self.min_energy_kwh
        if min_energy is None:
            min_energy = self.min_energy_kwh
        max_energy = profile.get("max_valid_energy_kwh") or profile.get("max_energy_kwh") or 3.0

        # A. Duration too short
        if run_minutes < min_valid_dur:
            flags.append("runtime_too_short")
        if run_minutes > max_dur:
            pass  # Optionally flag outlier but still allow completed
        # B. Energy
        if energy_kwh < min_energy:
            flags.append("energy_too_low")
        if energy_kwh > max_energy:
            flags.append("energy_too_high")
        # C. Door-open-first: do not assume valid; only completed + valid_for_learning if guards pass
        if transition_path == "door_opened_first":
            flags.append("door_opened_first")
        if transition_path == "unknown_programme" or (confirmed in ("unknown", "") or not confirmed):
            flags.append("unknown_programme")

        if "runtime_too_short" in flags and run_minutes < self.min_cycle_minutes:
            completion_class = "interrupted"
        elif "runtime_too_short" in flags or "energy_too_low" in flags or "energy_too_high" in flags:
            # User-confirmed programme: trust their selection; allow learning even if energy is above profile max.
            if user_conf and set(flags) <= {"energy_too_high"}:
                completion_class = "completed"
            else:
                completion_class = "suspect"
        elif transition_path == "door_opened_first" and ("runtime_too_short" in flags or "energy_too_low" in flags):
            completion_class = "suspect"
        else:
            completion_class = "completed"

        valid_for_learning = (
            completion_class == "completed"
            and "runtime_too_short" not in flags
            and "energy_too_low" not in flags
            and "unknown_programme" not in flags
        )
        if transition_path == "door_opened_first" and completion_class != "completed":
            valid_for_learning = False

        return {
            "completion_class": completion_class,
            "valid_for_learning": valid_for_learning,
            "validation_flags": flags,
            "end_reason": transition_path,
            "programme_key_used_for_validation": f"{confirmed}|{confirmed_temperature}" if (confirmed_temperature and self._programme_has_temperature(confirmed)) else confirmed,
        }

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
        learn_key = f"{confirmed}|{confirmed_temp}" if (confirmed_temp and self._programme_has_temperature(confirmed)) else confirmed
        if learn_key in self._learned_durations:
            old = self._learned_durations[learn_key]
            n = old["n"] - 1
            if n <= 0:
                del self._learned_durations[learn_key]
            else:
                avg_new = (old["avg"] * old["n"] - duration_min) / n
                self._learned_durations[learn_key] = {"n": n, "avg": avg_new}
        if learn_key in self._history_centroids and duration_min and duration_min > 0:
            old = self._history_centroids[learn_key]
            n = old["n"] - 1
            if n <= 0:
                del self._history_centroids[learn_key]
            else:
                rate_removed = energy_kwh / duration_min
                self._history_centroids[learn_key] = {
                    "rate": (old["rate"] * old["n"] - rate_removed) / n,
                    "heating_bursts": (old["heating_bursts"] * old["n"] - heating_bursts) / n,
                    "n": n,
                }
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
        self.programme_confirmed_by_user = bool(attrs.get("programme_confirmed_by_user"))
        self.confirmed_by_username = attrs.get("programme_confirmed_by") or None
        self.expected_dur_at_start = None
        # Prefer selector (manual duration) - never use stale/wrong expected_dur from entity.
        if self.confirm_entity:
            try:
                label = self.get_state(self.confirm_entity)
                if label and label not in ("Auto (unconfirmed)", "unknown", "unavailable"):
                    prog = self._LABEL_TO_KEY.get(label, "unknown")
                    temp = self._read_temperature_selector() if self._programme_has_temperature(prog) else None
                    if prog and prog != "unknown":
                        self.expected_dur_at_start = self._get_programme_duration(prog, temp, use_learned=False)
            except Exception:
                pass
        if self.expected_dur_at_start is None and self.detected_programme and self.detected_programme != "unknown":
            self.expected_dur_at_start = self._get_programme_duration(
                self.detected_programme, self.detected_temperature, use_learned=False
            )
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
        # Skip when user opened door first (skip_announce) or when we already verified standby/cadence break.
        if not skip_announce and self._pending_end_reason not in ("tail_to_standby", "tail_pattern_break"):
            ok, mean_w, peak_w = self._power_looks_like_cycle_end()
            if not ok:
                if mean_w is not None and peak_w is not None:
                    self.log(
                        f"Blocking transition to Unemptied: power pattern does not look like cycle end "
                        f"(mean={mean_w:.1f}W peak={peak_w:.1f}W; need mean≤{self.finish_power_gate_max_mean_w:.0f}W peak≤{self.finish_power_gate_max_peak_w:.0f}W or mean≤{self.finish_power_gate_off_max_mean_w:.0f}W peak≤{self.finish_power_gate_off_max_peak_w:.0f}W)",
                        level="INFO",
                    )
                return
        if self._should_change_state("Unemptied", force=force):
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
            self._save_cycle_feedback(
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
            )

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
            attributes["programme_confirmed_by_user"] = False
            attributes["programme_confirmed_by"] = ""
            attributes["expected_dur_at_start"] = ""

            # programme_confirmed_by_user is always False here (cleared for next load);
            # heating_bursts/last_door_closed_trusted can also be 0/False (cold programme,
            # no trusted door-close) -- AppDaemon 4.5.13 set_state bug, not ours; see
            # smart_cooling.py's _publish() for details.
            self._set_state_entity( state="Unemptied", attributes=attributes)

            self.programme_confirmed_by_user = False
            self.confirmed_by_username = None
            self.expected_dur_at_start = None
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
            if (not skip_announce and not self.door_lock_entity and self.sonos_notifier
                    and not self.notification_sent and announce_enabled):
                try:
                    self.sonos_notifier.notify(message=self.announce_message)
                    self.log("[TAIL] Washer announcement sent", level="INFO")
                    self.notification_sent = True
                except Exception as e:
                    self.log(f"Error sending notification: {e}", level="ERROR")

    def _transition_to_emptied(self, reason):
        """Transition to Emptied state (door open, user is emptying)."""
        if self._should_change_state("Emptied", force=True):  # Door event bypasses cooling
            energy_used = self._get_energy_used()
            run_minutes = self._get_run_duration_minutes()

            # Save feedback when we arrive in Emptied directly from Running (bypassing Unemptied).
            # This covers two paths:
            #   1. "cycle finished" reason  - explicit skip-Unemptied path
            #   2. Previous HA state is still "Running" - _transition_to_unemptied was blocked
            #      (e.g. by a transient exception) before _transition_to_emptied was called.
            # In both cases start_time and run_minutes are still set; feedback has not been saved yet.
            prev_ha_state = self.get_state(self.state_entity)
            came_from_running = (
                prev_ha_state == "Running"
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
                    self._save_cycle_feedback(
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
                    )
                except Exception as e:
                    self.log(f"Could not save feedback on Emptied transition: {e}", level="WARNING")

            self.state = "Emptied"
            attributes = {
                "reason": reason,
                "run_time_minutes": round(run_minutes, 1) if run_minutes > 0 else None
            }
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
        self.energy_start = None
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
        self.in_finishing_tail = False
        self.in_finishing_tail_entered_at = None
        self.last_tail_pulse_at = None
        self.door_fast_start_armed_until = None
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

        # Plug is reporting numbers again - stand down the dead-plug watchdog.
        if self._plug_outage_push_timer:
            self._safe_cancel_timer(self._plug_outage_push_timer)
            self._plug_outage_push_timer = None
        if self._plug_outage_pushed:
            self._plug_outage_pushed = False
            self._push_mobile("Power plug is reporting again - washer monitoring resumed.")

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
        self.expected_dur_at_start = None
        self.start_time = self._now_utc()
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
        }
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
                    attrs["expected_dur_at_start"] = self.expected_dur_at_start
                    self.log(f"Expected duration set from confirmed programme: {self.expected_dur_at_start:.0f} min", level="DEBUG")
            except Exception:
                pass
        if self.expected_dur_at_start is None:
            attrs["expected_dur_at_start"] = ""
        # elapsed_minutes/progress_pct/heating_bursts/max_power_w are always 0/0.0 at cycle start
        # (just reset); last_door_closed_trusted/programme_confirmed_by_user are commonly False too
        # (no trusted door-close yet, Auto-mode default) -- AppDaemon 4.5.13 set_state bug, not ours;
        # see smart_cooling.py's _publish() for details.
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

    def _handle_unavailable(self, entity, attribute, old, new, kwargs):
        """Handle entity becoming unavailable - use the real Off transition so all
        Running-state attributes are cleared and timers cancelled cleanly."""
        self.log(f"{entity} became unavailable ({new}), transitioning to Off", level="WARNING")
        self._transition_to_off(f"{entity} unavailable", force=True)
        if entity == self.power_sensor:
            self._begin_plug_outage_grace()

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
        """Identify individual cycles from history data.
        
        Uses POWER-BASED detection: cycle starts when power goes high (>=start_w),
        cycle ends when power drops low (<=stop_w) and stays low.
        This is the correct approach as it reflects actual washer operation, not user timing.
        """
        cycles = []
        running_periods = []
        
        # Primary method: Use POWER to identify actual cycle boundaries
        # Cycle starts: Power goes from low (<start_w) to high (>=start_w)
        # Cycle ends: Power drops from high to low (<=stop_w) and stays low
        if power_hist and len(power_hist) > 0:
            # Sort power readings by timestamp
            power_readings = []
            for entry in power_hist:
                try:
                    timestamp_str = entry.get("last_changed", "")
                    timestamp = _parse_utc(timestamp_str)
                    if timestamp is None:
                        continue
                    power_str = entry.get("state", "0")
                    if power_str not in ["unknown", "unavailable", None]:
                        power = float(power_str)
                        power_readings.append((timestamp, power))
                except (ValueError, AttributeError, TypeError):
                    continue
            
            power_readings.sort(key=lambda x: x[0])
            
            # Detect cycle boundaries based on power thresholds
            current_start = None
            low_power_count = 0
            high_power_count = 0
            low_power_start_time = None  # Track when low power period started
            
            for idx, (timestamp, power) in enumerate(power_readings):
                # Cycle start detection: power goes from low to high
                if current_start is None:
                    if power >= self.start_w:
                        high_power_count += 1
                        if high_power_count >= self.high_power_threshold:
                            # Confirmed cycle start - use first high power reading
                            start_idx = max(0, idx - self.high_power_threshold + 1)
                            current_start = power_readings[start_idx][0]
                            high_power_count = 0
                            low_power_count = 0
                            low_power_start_time = None
                    else:
                        high_power_count = 0
                
                # Cycle end detection: power drops and stays low
                elif current_start is not None:
                    if power <= self.stop_w:
                        if low_power_start_time is None:
                            low_power_start_time = timestamp
                        low_power_count += 1
                        
                        # Check if we've had enough consecutive low readings
                        if low_power_count >= self.low_power_threshold:
                            # Confirmed cycle end - use when low power period started
                            cycle_end = low_power_start_time
                            running_periods.append((current_start, cycle_end, "Off"))
                            current_start = None
                            low_power_count = 0
                            high_power_count = 0
                            low_power_start_time = None
                    else:
                        # Power recovered above stop_w - reset low power tracking
                        low_power_count = 0
                        low_power_start_time = None
        
        # Fallback: Use state transitions if power data insufficient
        if not running_periods and state_hist:
            current_start = None
            for entry in state_hist:
                state = entry.get("state", "")
                timestamp_str = entry.get("last_changed", "")
                try:
                    timestamp = _parse_utc(timestamp_str)
                    if timestamp is None:
                        continue
                    if state == "Running" and current_start is None:
                        current_start = timestamp
                    elif state in ("Off", "Unemptied") and current_start is not None:
                        running_periods.append((current_start, timestamp, state))
                        current_start = None
                except (ValueError, AttributeError):
                    continue
        
        # For each running period, calculate energy and duration
        for start, end, end_state in running_periods:
            # Find energy at start and end
            # Energy is cumulative, so we need the reading closest to each timestamp
            start_energy = None
            end_energy = None
            start_energy_time = None
            end_energy_time = None
            
            if energy_hist:
                for entry in energy_hist:
                    try:
                        timestamp = _parse_utc(entry.get("last_changed", ""))
                        if timestamp is None:
                            continue
                        energy = float(entry.get("state", 0))
                        
                        # Find energy reading closest to start time (within 10 minutes)
                        if abs((timestamp - start).total_seconds()) <= 600:  # 10 minutes
                            if start_energy is None or abs((timestamp - start).total_seconds()) < abs((start_energy_time - start).total_seconds()):
                                start_energy = energy
                                start_energy_time = timestamp
                        
                        # Find energy reading closest to end time (within 10 minutes)
                        if abs((timestamp - end).total_seconds()) <= 600:  # 10 minutes
                            if end_energy is None or abs((timestamp - end).total_seconds()) < abs((end_energy_time - end).total_seconds()):
                                end_energy = energy
                                end_energy_time = timestamp
                    except (ValueError, AttributeError, TypeError):
                        continue
            
            if start_energy is not None and end_energy is not None:
                energy_used = end_energy - start_energy
                duration_min = (end - start).total_seconds() / 60
                
                cycles.append({
                    "start": start,
                    "end": end,
                    "duration_minutes": duration_min,
                    "energy_kwh": energy_used,
                    "end_state": end_state,
                    "start_energy": start_energy,
                    "end_energy": end_energy
                })
        
        return cycles

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
                # "—" (em dash) = no choice made; "Cold" = cold/snowflake (no heating)
                if val.strip() in ("—", "–", "-"):
                    return None
                if val.strip().lower() == "cold":
                    return "cold"
                return val if "°" in val else f"{val}°C"
        except Exception:
            pass
        return None

    @staticmethod
    def _temp_for_storage(temp):
        """Return temperature for JSON storage (no degree symbol). '40°C' -> '40'; 'cold' -> 'cold'; None/empty -> None."""
        if not temp or not isinstance(temp, str):
            return None
        s = temp.strip()
        if s.lower() == "cold":
            return "cold"
        s = s.replace("°C", "").replace("°", "").strip()
        return s if s else None

    @staticmethod
    def _temp_from_storage(s):
        """Return temperature for internal use (e.g. profile lookup). '40' or '40°C' -> '40°C'; 'cold' -> 'cold'; None/empty -> None."""
        if s is None or (isinstance(s, str) and not s.strip()):
            return None
        s = str(s).strip()
        if s.lower() == "cold":
            return "cold"
        if "°" in s:
            return s
        return f"{s}°C" if s else None

    def _classify_from_history(self, run_min: float, energy_used: float, heating_bursts: int):
        """Use power-pattern match to historical confirmed cycles when ambiguous.

        Centroids are keyed by "prog|temp" strings.
        Returns (programme, temperature) tuple or None.
        """
        if not self._history_centroids or run_min < 5:
            return None
        rate_curr = energy_used / run_min
        best_key = None
        best_dist = float("inf")
        for key, c in self._history_centroids.items():
            rate_dist = 500 * abs(rate_curr - c["rate"])
            burst_dist = 0.5 * abs(heating_bursts - c["heating_bursts"])
            dist = rate_dist + burst_dist
            if dist < best_dist:
                best_dist = dist
                best_key = key
        if best_key is None or best_dist > 2.0:
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

    def _get_guard_duration(self, tick_prog=None, tick_temp=None, tick_class=None):
        """Best duration for 85% guards: prefer user-confirmed selector, else frozen, else classified, else max.
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
    _LABEL_TO_KEY = {
        # Current HA input_select options (short names, no temperature suffix)
        "Ekspres":   "ekspres",
        "Uld":       "uld",
        "Bomuld":    "bomuld",
        "Finvask":   "finvask",
        "Strygelet": "strygelet",
        "ECO":       "eco",
        "Mørkt/Denim": "morkt_denim",
        "Outdoor":   "outdoor",
        "Imprægnering": "impraegnering",
        "Pumpe/Centrifugering": "pumpe_centrifugering",
        "Kun skyl/stivelse": "kun_skyl_stivelse",
        # Legacy options with temperature suffix (backwards compatibility)
        "Ekspres 20":   "ekspres",
        "Uld 30":       "uld",
        "Bomuld 20":    "bomuld",
        "Bomuld 60":    "bomuld",
        "Finvask 40":   "finvask",
        "Strygelet 30": "strygelet",
        "ECO 40-60":    "eco",
    }

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
        learned = self._learned_durations.get(learn_key)
        if learned is None:
            return manual
        n = learned["n"]
        if n < 1:
            return manual
        avg = learned["avg"]
        if n == 1:
            return round(0.30 * avg + 0.70 * manual)
        if n == 2:
            return round(0.50 * avg + 0.50 * manual)
        alpha = min(0.9, 0.6 + (n - 3) * (0.30 / 7))
        return round(alpha * avg + (1 - alpha) * manual)

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

        buckets: dict = {}   # {"prog|temp": {"durations": [], "correct": int, "total": int}}
        signatures = []      # (learn_key, rate kWh/min, heating_bursts)
        skipped_unconfirmed = 0

        for rec in cycles:
            confirmed = rec.get("confirmed", "")
            predicted = rec.get("predicted", "")
            conf_temp = self._temp_from_storage(rec.get("confirmed_temperature"))
            pred_temp = self._temp_from_storage(rec.get("predicted_temperature"))
            dur = rec.get("duration_min")
            user_confirmed = rec.get("programme_user_confirmed", rec.get("user_confirmed", False))
            valid_for_learning = rec.get("valid_for_learning", False)  # Only learn from validated cycles

            if not confirmed or not isinstance(dur, (int, float)):
                continue
            # Only use user-confirmed cycles for learning - unconfirmed (guessed) programmes
            # can be wrong (e.g. false finishes with wrong duration).
            if not user_confirmed:
                skipped_unconfirmed += 1
                continue
            # Only use records with valid_for_learning == True for learned durations and centroids.
            if not valid_for_learning:
                continue

            learn_key = f"{confirmed}|{conf_temp}" if (conf_temp and self._programme_has_temperature(confirmed)) else confirmed
            pred_key = f"{predicted}|{pred_temp}" if (pred_temp and self._programme_has_temperature(predicted)) else predicted

            if learn_key not in buckets:
                buckets[learn_key] = {"durations": [], "correct": 0, "total": 0, "prog": confirmed, "temp": conf_temp}
            buckets[learn_key]["durations"].append(dur)
            buckets[learn_key]["total"] += 1
            if learn_key == pred_key:
                buckets[learn_key]["correct"] += 1

            profile = self._get_profile(confirmed, conf_temp)
            if profile.get("heats") and dur and dur > 0:
                energy_kwh = rec.get("energy_kwh")
                bursts = rec.get("heating_bursts")
                if isinstance(energy_kwh, (int, float)) and energy_kwh >= 0:
                    rate = energy_kwh / dur
                    signatures.append((learn_key, rate, bursts if isinstance(bursts, (int, float)) else 0))

        # Build history centroids keyed by "prog|temp"
        sig_by_key: dict = {}
        for key, rate, bursts in signatures:
            sig_by_key.setdefault(key, []).append((rate, bursts))
        for key, pts in sig_by_key.items():
            n = len(pts)
            self._history_centroids[key] = {
                "rate": sum(r for r, _ in pts) / n,
                "heating_bursts": sum(b for _, b in pts) / n,
                "n": n,
            }

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
        profile_version = "1"
        validation_version = "2"
        counts = {"completed": 0, "interrupted": 0, "suspect": 0, "learnable": 0, "quarantined": 0, "unchanged": 0}
        for rec in cycles:
            if rec.get("completion_class") and rec.get("valid_for_learning") is not None:
                if rec.get("profile_version") == profile_version and rec.get("validation_version") == validation_version:
                    counts["unchanged"] += 1
                    continue
            dur = rec.get("duration_min")
            if not isinstance(dur, (int, float)):
                continue
            confirmed = rec.get("confirmed", "")
            conf_temp = self._temp_from_storage(rec.get("confirmed_temperature"))
            pred = rec.get("predicted", "")
            pred_temp = self._temp_from_storage(rec.get("predicted_temperature"))
            transition_path = rec.get("end_reason") or rec.get("transition_path") or "low_power_detected"
            if transition_path not in ("user_cycle_end", "anti_crease_pattern", "low_power_detected", "door_opened_first", "tail_to_standby", "tail_pattern_break"):
                transition_path = "low_power_detected"
            classification = self._classify_cycle_completion(
                run_minutes=float(dur),
                energy_kwh=float(rec.get("energy_kwh", 0) or 0),
                heating_bursts=int(rec.get("heating_bursts", 0) or 0),
                max_power_w=float(rec.get("max_power_w", 0) or 0),
                predicted=pred,
                predicted_temperature=pred_temp,
                confirmed=confirmed,
                confirmed_temperature=conf_temp,
                transition_path=transition_path,
                spin_rpm=rec.get("spin_rpm"),
                user_confirmed_override=rec.get("programme_user_confirmed", rec.get("user_confirmed", False)),
            )
            rec["completion_class"] = classification["completion_class"]
            rec["valid_for_learning"] = classification["valid_for_learning"]
            rec["validation_flags"] = classification["validation_flags"]
            rec["transition_path"] = classification["end_reason"]
            rec["programme_key_used_for_validation"] = classification.get("programme_key_used_for_validation", "")
            rec["profile_version"] = profile_version
            rec["validation_version"] = validation_version
            counts[classification["completion_class"]] = counts.get(classification["completion_class"], 0) + 1
            if classification["valid_for_learning"]:
                counts["learnable"] += 1
            else:
                counts["quarantined"] += 1
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
        """
        import json
        import os

        record = {
            "ts": self._format_local(self._now_utc()),
            "duration_min": round(duration_min, 1),
            "energy_kwh": round(energy_kwh, 3),
            "heating_bursts": heating_bursts,
            "max_power_w": round(max_power_w, 0),
            "predicted": predicted,
            "predicted_temperature": self._temp_for_storage(predicted_temperature),
            "confirmed": confirmed,
            "confirmed_temperature": self._temp_for_storage(confirmed_temperature),
            "programme_user_confirmed": user_confirmed,
            "spin_user_confirmed": spin_user_confirmed,
        }
        if confirmed_by:
            record["confirmed_by"] = confirmed_by
        if spin_rpm is not None:
            record["spin_rpm"] = spin_rpm
        if duration_source:
            record["duration_source"] = duration_source
        if end_reason:
            record["end_reason"] = end_reason
        if idle_min is not None and idle_min >= 0:
            record["idle_min"] = round(idle_min, 1)
        if effective_end_at:
            record["effective_end_at"] = effective_end_at
        if detected_at:
            record["detected_at"] = detected_at
        if completion_class:
            record["completion_class"] = completion_class
        if valid_for_learning is not None:
            record["valid_for_learning"] = valid_for_learning
        if validation_flags is not None:
            record["validation_flags"] = list(validation_flags)
        if transition_path:
            record["transition_path"] = transition_path
        if programme_key_used_for_validation:
            record["programme_key_used_for_validation"] = programme_key_used_for_validation
        if profile_version:
            record["profile_version"] = profile_version
        if validation_version:
            record["validation_version"] = validation_version
        if selected_options is not None and selected_options:
            record["selected_options"] = dict(selected_options)

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
            learn_key = f"{confirmed}|{confirmed_temperature}" if (confirmed_temperature and self._programme_has_temperature(confirmed)) else confirmed
            prev = self._learned_durations.get(learn_key, {"n": 0, "avg": duration_min})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + duration_min) / n_new
            self._learned_durations[learn_key] = {"n": n_new, "avg": avg_new}

            profile = self._get_profile(confirmed, confirmed_temperature)
            if profile.get("heats") and duration_min and duration_min > 0:
                centroid_key = learn_key
                rate_new = energy_kwh / duration_min
                if centroid_key not in self._history_centroids:
                    self._history_centroids[centroid_key] = {"rate": rate_new, "heating_bursts": float(heating_bursts), "n": 1}
                else:
                    old = self._history_centroids[centroid_key]
                    n = old["n"] + 1
                    self._history_centroids[centroid_key] = {
                        "rate": (old["rate"] * old["n"] + rate_new) / n,
                        "heating_bursts": (old["heating_bursts"] * old["n"] + heating_bursts) / n,
                        "n": n,
                    }

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
        label = self._get_profile(prog_key, conf_temp).get("label", prog_key)
        self.log(
            f"Updated last feedback: programme_user_confirmed=True, valid_for_learning={classification['valid_for_learning']} ({label})",
            level="INFO",
        )

    def _parse_spin_rpm(self, value: str) -> int | None:
        """Parse spin speed from input_select state. Returns rpm (0 = no spin) or None."""
        if not value or value.strip() in ("—", ""):
            return None
        value = value.strip().lower()
        if "no spin" in value or "flydeslut" in value:
            return 0
        import re
        m = re.search(r"(\d+)\s*rpm", value)
        if m:
            return int(m.group(1))
        if value.isdigit():
            return int(value)
        return None

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
    
    def _check_energy_finish(self, kwargs):
        """Check if energy consumption has stopped (cycle finished)."""
        current_state = self.get_state(self.state_entity)

        if current_state != "Running":
            self.energy_check_timer = None
            return

        # Compute programme classification once per tick to avoid redundant get_state calls.
        _tick_prog, _tick_temp = self._classify_programme()
        _pred_prog, _pred_temp = self._classify_programme(energy_signature_only=True)
        _tick_class = (_tick_prog, _tick_temp)

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
                self.start_time > last_door):
            gap_seconds = (self.start_time - last_door).total_seconds()
            if gap_seconds >= 60:
                self.log(
                    f"Correcting start_time: was {self._strftime_local(self.start_time)} (after door close) "
                    f"-> {self._strftime_local(last_door)}",
                    level="INFO",
                )
                self.start_time = last_door
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
                            self.log(
                                f"Correcting start_time to entity last_changed (cycle 2 start) "
                                f"(was {self._strftime_local(self.start_time)}, now {self._strftime_local(last_changed_dt)})",
                                level="INFO",
                            )
                            self.start_time = last_changed_dt
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
        # Freeze expected duration at first confident classification so mid-cycle
        # selector changes can never weaken the too-early guard. Placed outside the
        # "changed" block so it also fires when restore pre-set detected_programme
        # to the same value (no change event) but expected_dur_at_start was lost.
        if new_prog != "unknown" and self.expected_dur_at_start is None:
            dur = self._get_programme_duration(new_prog, new_temp, use_learned=False)
            if dur:
                self.expected_dur_at_start = dur
                self.log(f"Frozen expected_dur_at_start: {dur:.0f} min", level="DEBUG")

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
        # cycle_complete/heating_bursts/progress_pct/estimated_remaining_min/last_door_closed_trusted/
        # programme_confirmed_by_user can all legitimately be False/0 on a normal mid-cycle tick (still
        # running, pre-heating, near start/end of countdown, Auto mode, no trusted door-close) --
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

            # Standby backstop: if instantaneous power is 0W for 3+ minutes,
            # the machine is completely off - force finish regardless of the
            # rolling energy window (which lags due to REF_WINDOW_S).
            current_power = self._get_current_power()
            if current_power <= 0.0:
                if self._zero_power_since is None:
                    self._zero_power_since = now
                zero_min = (now - self._zero_power_since).total_seconds() / 60
                if zero_min >= 3.0:
                    run_min = (now - self.start_time).total_seconds() / 60 if self.start_time else 0
                    guard_dur = self._get_guard_duration(_tick_prog, _tick_temp, _tick_class)
                    if self._meets_finish_time_guards(run_min, guard_dur or 0):
                        self.log(
                            f"Standby backstop: power 0W for {zero_min:.1f}min - machine is off",
                            level="INFO",
                        )
                        if self._is_valid_completed_cycle():
                            self._transition_to_unemptied()
                            return
                    else:
                        self.log(
                            f"Standby backstop: 0W for {zero_min:.1f}min but finish time guards not met (run {run_min:.0f}min) - skipping",
                            level="DEBUG",
                        )
                    # Invalid cycle (e.g. false-start or ghost Running state) with sustained
                    # zero power - machine is clearly off, go directly to Off.
                    if zero_min >= 5.0:
                        self.log(
                            f"Standby backstop: cycle invalid + 0W for {zero_min:.1f}min - forcing Off",
                            level="WARNING",
                        )
                        self._transition_to_off("Standby backstop: invalid cycle with sustained zero power")
                        return
                    self.log("Standby backstop but cycle validation failed - keep checking", level="WARNING")
            else:
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
