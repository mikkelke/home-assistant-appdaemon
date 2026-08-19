"""
dryer_policy.py - Dryer transition table, named guards/actions, KeepFreshDetector, and the
physics kept per-appliance (spec section 4): classifier, EMA-blended duration, 80% guard,
valid-cycle, history correction, overrun ETA, confirmed-key derivation, feedback record build,
plus boot-restore precedence+corroboration+reconcile (spec sections 4.3, 6, 8).

Every function/table row here is a faithful port of dryer_monitor.py's own logic, reimplemented
(not imported - dryer_monitor.py is an AppDaemon app module, not an importable library; F5) and
cited by CURRENT line number (post the 2026-08-19 F2 boot-self-heal-freshness hotfix; the
self-heal region shifted ~50 lines from an earlier read - re-grep before trusting an old line ref).

CONFIRM-TIMER OWNERSHIP: start-confirm/finish-confirm/door_fast_start arm+cancel live in
appliance_detectors.py's PowerStartDetector/PowerEndDetector, not here - table actions below
therefore do less than spec section 4's literal "Action" column per row; the detector already
performed the arm/cancel as a side effect of emitting the evidence (see that module's own note).

RECONCILE is NOT in dryer_monitor.py at all (spec section 8 introduces it fresh, engine-native).
Implemented as ordinary table rows on RUNNING/PAUSED so it flows through the same submit()
pipeline as everything else; washer_monitor.py:1276-1348 is the faithfully-ported reference,
including its "force push, never Sonos" invariant (D2 there) - simpler and stronger than the
finish_anchor_override arithmetic spec section 8 sketches, so used directly instead (see
DryerPolicy._reconcile_finish's docstring; the only spec deviation in this file, and a narrow one).

DryerPolicy is a stateful object: cycle physics (start_time, energy_start, detected_programme,
...) lives here, on self, mirroring dryer_monitor.py's own self.* attributes 1:1 - the engine
(appliance_fsm.py) holds no appliance state at all (state/published/hypothesis/cycle_id only).
to_store_fields()/restore_from() are dryer_shadow.py's only touch points for that state, so the
shadow app stays a thin host, per spec section 7.1.

Pure stdlib, Python 3.12+, ASCII-only log/message strings.
"""

from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from appliance_fsm import BYPASS, Detector, Evidence, EvidenceType, RESPECT, Row, State
    from appliance_detectors import WatchdogTimer
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from appliance_fsm import BYPASS, Detector, Evidence, EvidenceType, RESPECT, Row, State
    from appliance_detectors import WatchdogTimer

E = EvidenceType
S = State

# ---------------------------------------------------------------------------------------------
# Config defaults - dryer.yaml's knob surface, reduced per spec section 9 (25 behavioral knobs +
# a selectors: sub-block). high_power_threshold dropped (F4, dead - set, never read, upstream);
# power_unavailable_off_after_seconds/power_unavailable_push_after_seconds merged into
# power_unavailable_grace_s (both default 180, same meaning); restore_corroboration_window_minutes
# and announce_freshness_minutes are the two spec-mandated additions (both already present on the
# live dryer.yaml/dryer_monitor.py as of the 2026-08-19 hotfix, at the same defaults - reused).
# ---------------------------------------------------------------------------------------------

DEFAULTS = {
    "start_w": 8.0, "stop_w": 5.0, "run_for": 60, "stop_for": 60, "main_cycle_power": 400.0,
    "door_close_fast_start_window_s": 600, "door_close_fast_confirm_s": 12,
    "cooling_period": 600, "fill_window_minutes": 60, "min_cycle_minutes": 60,
    "min_energy_kwh": 0.05, "pause_timeout_minutes": 10, "max_running_hours": 5.0,
    "unemptied_timeout_hours": 24.0, "emptied_timeout_minutes": 30.0,
    "energy_active_watts": 100.0, "overrun_floor_min": 10,
    "store_max_downtime_hours": 12.0, "boot_future_start_skew_s": 60,
    "reset_programme_on_idle": True, "reset_time_minutes_on_idle": False,
    "announce_message": "Dryer is ready to be emptied",
    "restore_corroboration_window_minutes": 10.0, "announce_freshness_minutes": 20.0,
    "power_unavailable_grace_s": 180,
    "selectors": {
        "programme_unconfirmed_option": "Auto (unconfirmed)",
        "dryness_unselected_option": "—",  # em-dash - see spec section 9's ASCII-placeholder note
        "time_minutes_idle_default": "20",
    },
}


# ---------------------------------------------------------------------------------------------
# Programme profiles / learned-feedback loading (dryer_monitor.py:907-935, 936-963) - pure I/O
# helpers, no self; `log` is an optional AppDaemon-shaped callable, matching cycle_store.py's
# own convention.
# ---------------------------------------------------------------------------------------------

_PROFILE_DEFAULTS = {
    "unknown": {"label": "Unknown", "duration_min": 120, "max_energy_kwh": 2.0},
    "finish_uld": {"label": "Finish uld", "duration_min": 5, "max_energy_kwh": 0.02, "is_real": False},
}


def _noop_log(message, level="INFO"):
    return None


def load_programme_profiles(path, log=None):
    """dryer_monitor.py:907-935, ported verbatim minus the self.args/class-attribute plumbing."""
    log = log or _noop_log
    try:
        import yaml

        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        profiles = data.get("programmes", {})
        if not profiles:
            return dict(_PROFILE_DEFAULTS)
        merged = dict(_PROFILE_DEFAULTS)
        for key, val in profiles.items():
            if isinstance(val, dict):
                merged[key] = {**merged.get(key, {}), **val}
        log(f"Loaded {len(profiles)} programme profiles from {path}", level="INFO")
        return merged
    except FileNotFoundError:
        log(f"Programme file {path} not found - using defaults", level="WARNING")
        return dict(_PROFILE_DEFAULTS)
    except Exception as exc:
        log(f"Failed to load {path}: {exc} - using defaults", level="ERROR")
        return dict(_PROFILE_DEFAULTS)


def load_learned_durations(feedback_path, log=None):
    """dryer_monitor.py:936-963, ported verbatim. Only human-confirmed cycles feed the EMA."""
    log = log or _noop_log
    learned = {}
    if not os.path.exists(feedback_path):
        return learned
    try:
        import json

        with open(feedback_path, "r") as f:
            data = json.load(f)
    except Exception as e:
        log(f"Could not load feedback {feedback_path}: {e}", level="WARNING")
        return learned
    for c in data.get("cycles") or []:
        if not c.get("programme_confirmed_by_human", c.get("programme_user_confirmed", False)):
            continue
        prog = c.get("confirmed", "")
        dur = c.get("duration_min")
        if not prog or prog == "unknown" or dur is None or dur <= 0:
            continue
        prev = learned.get(prog, {"n": 0, "avg": float(dur)})
        n_new = prev["n"] + 1
        learned[prog] = {"n": n_new, "avg": (prev["avg"] * prev["n"] + float(dur)) / n_new}
    return learned


# ---------------------------------------------------------------------------------------------
# Classifier + duration/guard (dryer_monitor.py:965-1051) - pure functions of (run_min, energy).
# ---------------------------------------------------------------------------------------------

def classify_programme(run_min, energy_kwh):
    """dryer_monitor.py:965-1014, ported verbatim (energy+runtime bands, heat-pump dryer)."""
    if run_min < 5:
        return "unknown"
    if energy_kwh < 0.05 and run_min < 10:
        return "finish_uld"
    if 0.40 <= energy_kwh <= 0.55 and 55 <= run_min <= 75:
        if run_min <= 62 and energy_kwh <= 0.48:
            return "skjorter__skabstoert"
        if run_min >= 64:
            return "strygelet__skabstoert"
        return "finvask__skabstoert__skane_fixed"
    if 0.70 <= energy_kwh <= 1.0 and 90 <= run_min <= 105:
        if energy_kwh <= 0.78 and run_min <= 97:
            return "impraegnering__skabstoert"
        return "ekspres__skabstoert"
    if 0.88 <= energy_kwh <= 1.05 and 108 <= run_min <= 125:
        return "denim__skabstoert"
    if 0.80 <= energy_kwh <= 1.30 and 80 <= run_min <= 125:
        return "bomuld__strygetoert"
    if 1.25 <= energy_kwh <= 1.55 and 115 <= run_min <= 145:
        return "bomuld__skabstoert"
    if energy_kwh >= 1.65 and run_min >= 155:
        return "bomuld__skabstoert__skane"
    if energy_kwh >= 1.55 and run_min >= 140:
        return "bomuld_eco__skabstoert"
    if 0.90 <= energy_kwh <= 1.0 and 90 <= run_min <= 100:
        return "ekspres__skabstoert"
    if energy_kwh < 0.5 and run_min < 70:
        return "skjorter__skabstoert"
    if energy_kwh < 0.9 and run_min < 110:
        return "ekspres__skabstoert"
    if energy_kwh < 1.2:
        return "bomuld__strygetoert"
    if energy_kwh < 1.6:
        return "bomuld__skabstoert"
    return "bomuld_eco__skabstoert"


def get_profile(profiles, programme):
    """dryer_monitor.py:901-905."""
    return profiles.get(programme, profiles.get("unknown", {}))


def programme_duration(profiles, learned, prog, use_learned=True):
    """Expected duration in minutes, EMA-blended toward the learned average as confirmed-cycle
    count grows (dryer_monitor.py:1016-1033). use_learned=False for guard-duration callers."""
    profile = get_profile(profiles, prog)
    manual = profile.get("duration_min") or 120
    if not use_learned:
        return manual
    lrn = learned.get(prog)
    if lrn is None or lrn["n"] < 1:
        return manual
    n, avg = lrn["n"], lrn["avg"]
    if n == 1:
        return round(0.30 * avg + 0.70 * manual)
    if n == 2:
        return round(0.50 * avg + 0.50 * manual)
    alpha = min(0.9, 0.6 + (n - 3) * (0.30 / 7))
    return round(alpha * avg + (1 - alpha) * manual)


def guard_duration(profiles, learned, tick_prog, expected_dur_at_start, max_running_hours):
    """Duration the 80% guard is measured against - manual (never learned) duration, anchored to
    the RESTORED/STARTED programme via expected_dur_at_start when available, never a live
    re-classify (dryer_monitor.py:1043-1051; anti-regression test_dryer_restart_survival.py:
    643-687; spec R7/G_past_guard)."""
    if tick_prog and tick_prog != "unknown":
        d = programme_duration(profiles, learned, tick_prog, use_learned=False)
        if d:
            return d
    if expected_dur_at_start is not None:
        return expected_dur_at_start
    profile = get_profile(profiles, tick_prog or "unknown")
    dur = profile.get("duration_min")
    return dur if dur else int(max_running_hours * 60)


def is_valid_completed_cycle(run_min, energy_kwh, keep_fresh, min_cycle_minutes, min_energy_kwh):
    """dryer_monitor.py:883-899. Keep-fresh detection short-circuits (the pattern itself is proof)."""
    if keep_fresh:
        return True
    return run_min >= min_cycle_minutes and energy_kwh >= min_energy_kwh


def overrun_eta(elapsed, dur, keep_fresh, current_power, main_cycle_power, overrun_floor_min):
    """dryer_monitor.py:1053-1085, verbatim (already a pure @staticmethod there). Returns
    (remaining_min, progress_pct, overrun_min); overrun_min is always >= 0."""
    raw_remaining = dur - elapsed
    if raw_remaining > 0:
        progress = min(100, round(100 * elapsed / dur)) if dur > 0 else 0
        return raw_remaining, progress, 0
    overrun = -raw_remaining
    if keep_fresh:
        return 2, 100, overrun
    if current_power >= main_cycle_power:
        return overrun_floor_min, 99, overrun
    return 5, 99, overrun


# ---------------------------------------------------------------------------------------------
# History correction (dryer_monitor.py:1172-1305) - needs ctx.get_history/ctx.now, so these take
# `ctx` rather than being fully pure. `_flatten_history` matches dryer_monitor.py:1172-1178.
# ---------------------------------------------------------------------------------------------

def _flatten_history(hist, entity_id=None):
    if isinstance(hist, dict):
        hist = hist.get(entity_id or "", []) or hist.get("history", []) if entity_id else next(iter(hist.values()), [])
    if isinstance(hist, list) and hist and isinstance(hist[0], list):
        return hist[0]
    return hist if isinstance(hist, list) else []


def _parse_hist_ts(s):
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    if "+" not in s[-7:] and "-" not in s[-7:]:
        s = s + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def estimate_cycle_end_from_history(ctx, energy_sensor, start_time, min_cycle_minutes,
                                     energy_active_watts, expected_duration_min=None):
    """dryer_monitor.py:1186-1242, ported to take its inputs as arguments instead of self.*."""
    if not start_time or not energy_sensor:
        return None
    try:
        end_time = ctx.now()
        hist = _flatten_history(ctx.get_history(energy_sensor, start_time=start_time, end_time=end_time), energy_sensor)
        points = []
        for entry in hist:
            ts = _parse_hist_ts(entry.get("last_changed") or entry.get("last_updated"))
            s = entry.get("state")
            if ts is None or s in (None, "unknown", "unavailable", ""):
                continue
            try:
                points.append((ts, float(s)))
            except (ValueError, TypeError):
                continue
        if len(points) < 2:
            return None
        points.sort(key=lambda x: x[0])
        run_minutes_max = (end_time - start_time).total_seconds() / 60
        candidates = []
        for i in range(1, len(points)):
            t1, e1 = points[i - 1]
            t2, e2 = points[i]
            delta_s = (t2 - t1).total_seconds()
            if delta_s < 10:
                continue
            implied_w = ((e2 - e1) * 1000) / (delta_s / 3600)
            if implied_w > energy_active_watts:
                candidates.append(t2)
        if not candidates:
            return None
        if expected_duration_min and expected_duration_min > 0:
            best_end, best_diff = None, float("inf")
            for t in candidates:
                dur = (t - start_time).total_seconds() / 60
                if dur < min_cycle_minutes or dur > run_minutes_max:
                    continue
                diff = abs(dur - expected_duration_min)
                if diff < best_diff:
                    best_diff, best_end = diff, t
            if best_end:
                return best_end + timedelta(minutes=2)
        return candidates[-1] + timedelta(minutes=2)
    except Exception as e:
        ctx.log(f"Could not estimate cycle end from history: {e}", level="DEBUG")
        return None


def door_open_edge_since(ctx, door_sensor, since_dt):
    """dryer_monitor.py:1244-1274. Standard polarity only (on/open=open) - the dryer contact has
    only ever been wired one way (no door_sensor_inverted knob to consult here, unlike
    DoorEdgeDetector's evidence-emission path). Fails OPEN (returns False) on any lookup error -
    a broken recorder query must never SUPPRESS the normal finish path by accident."""
    if not door_sensor or since_dt is None:
        return False
    try:
        hist = _flatten_history(ctx.get_history(door_sensor, start_time=since_dt, end_time=ctx.now()), door_sensor)
        for entry in hist:
            state = entry.get("state")
            if state in (None, "unknown", "unavailable"):
                continue
            t = _parse_hist_ts(entry.get("last_changed") or entry.get("last_updated"))
            if t is None or t <= since_dt:
                continue
            if state in ("on", "open"):
                return True
        return False
    except Exception as e:
        ctx.log(f"Door-open history check failed: {e}", level="DEBUG")
        return False


def correct_duration(ctx, run_minutes_wall, *, energy_sensor, start_time, min_cycle_minutes,
                      energy_active_watts, duration_hint, log_prefix=""):
    """dryer_monitor.py:1291-1305. Returns (run_minutes, duration_source)."""
    actual_end = estimate_cycle_end_from_history(
        ctx, energy_sensor, start_time, min_cycle_minutes, energy_active_watts, expected_duration_min=duration_hint
    )
    if actual_end and start_time:
        actual = (actual_end - start_time).total_seconds() / 60
        if min_cycle_minutes <= actual <= run_minutes_wall:
            delta = run_minutes_wall - actual
            if delta > 1.0:
                ctx.log(f"{log_prefix}Using HA history: {actual:.1f} min (detection was {delta:.0f} min late)", level="INFO")
            return actual, "history_corrected"
    return run_minutes_wall, None


# ---------------------------------------------------------------------------------------------
# Confirmed-key derivation (dryer_monitor.py:1307-1399) - reads the split UI selectors via
# ctx.get_state/ctx.config. Danish programme labels ported verbatim.
# ---------------------------------------------------------------------------------------------

_DRYNESS_TO_SEGMENT = {
    "Ekstra tørt": "ekstra_toert", "Skabstørt": "skabstoert",
    "Strygetoert": "strygetoert", "Strygetørt": "strygetoert", "Rulletoert": "rulletoert", "Rulletørt": "rulletoert",
}
_PROGRAMMES_REQUIRING_DRYNESS = frozenset((
    "Finvask", "Udglatning", "Bomuld", "Strygelet", "Skjorter", "Denim", "Ekspres", "Sengetoej", "Sengetøj",
))


def get_confirmed_programme_key(ctx):
    """dryer_monitor.py:1319-1392, ported verbatim (selector reads via ctx.get_state/ctx.config)."""
    programme_entity = ctx.config.get("programme_entity")
    if not programme_entity:
        return None
    prog = (ctx.get_state(programme_entity) or "").strip()
    unconfirmed = ctx.config.get("selectors", {}).get("programme_unconfirmed_option", "Auto (unconfirmed)")
    if not prog or prog in (unconfirmed, "unknown"):
        return None
    dryness_entity = ctx.config.get("dryness_entity")
    dryness = (ctx.get_state(dryness_entity) or "").strip() if dryness_entity else ""
    skane_entity = ctx.config.get("skane_plus_entity")
    skane_on = ctx.get_state(skane_entity) == "on" if skane_entity else False
    time_entity = ctx.config.get("time_minutes_entity")
    time_val = (ctx.get_state(time_entity) or "").strip() if time_entity else ""

    if prog in ("Bomuld eco", "Bomuld eco - Skabstoert", "Bomuld eco - Skabstørt"):
        return "bomuld_eco__skabstoert"
    if prog == "Finish uld":
        return "finish_uld"
    if prog == "Impraegnering" or prog == "Imprægnering":
        return "impraegnering__skabstoert"
    if prog == "Varm luft":
        try:
            digits = "".join(c for c in str(time_val or "90") if c.isdigit())
            min_val = int(digits) if digits else 90
            min_val = max(20, min(120, ((min_val + 5) // 10) * 10))
        except (ValueError, TypeError):
            min_val = 90
        seg = f"varm_luft__{min_val:03d}min"
        return f"{seg}__skane" if skane_on else seg

    if prog not in _PROGRAMMES_REQUIRING_DRYNESS:
        return None
    if not dryness or dryness not in _DRYNESS_TO_SEGMENT:
        return None
    dry_seg = _DRYNESS_TO_SEGMENT[dryness]

    if prog == "Finvask":
        return "finvask__strygetoert__skane_fixed" if dry_seg == "strygetoert" else "finvask__skabstoert__skane_fixed"
    if prog == "Udglatning":
        return "udglatning__strygetoert__skane_fixed" if dry_seg == "strygetoert" else "udglatning__skabstoert__skane_fixed"

    base = {"Bomuld": "bomuld", "Strygelet": "strygelet", "Skjorter": "skjorter", "Denim": "denim"}.get(prog)
    if base is None and prog in ("Ekspres", "Sengetoej", "Sengetøj"):
        base = "ekspres" if prog == "Ekspres" else "sengetoej"
        skane_on = False  # Ekspres/Sengetoej have no Skane+
    if base:
        return f"{base}__{dry_seg}" + ("__skane" if skane_on else "")
    return None


def get_confirmed_from_selector(ctx, predicted):
    """dryer_monitor.py:1394-1399. Returns (confirmed_key, programme_confirmed_by_human)."""
    key = get_confirmed_programme_key(ctx)
    return (predicted, False) if key is None else (key, True)


def build_feedback_record(*, ts_local, predicted, confirmed, duration_min, energy_kwh, max_power_w,
                           programme_confirmed_by_human=False, duration_source=None, end_reason=None,
                           idle_min=None):
    """dryer_monitor.py:1401-1450's record shape, minus the file I/O (ActionSink.save_feedback
    owns persistence now - see appliance_fsm.py's exactly-once path, spec section 6)."""
    record = {
        "ts": ts_local, "duration_min": round(duration_min, 1), "energy_kwh": round(energy_kwh, 3),
        "predicted": predicted, "confirmed": confirmed,
        "programme_confirmed_by_human": programme_confirmed_by_human, "max_power_w": round(max_power_w, 0),
    }
    if duration_source:
        record["duration_source"] = duration_source
    if end_reason:
        record["end_reason"] = end_reason
    if idle_min is not None and idle_min >= 0:
        record["idle_min"] = round(idle_min, 1)
    return record


# ---------------------------------------------------------------------------------------------
# KeepFreshDetector (dryer_monitor.py:2052-2116) - dryer-specific, hand-tuned thresholds (U3, not
# generalized). Buffers power samples while RUNNING/ENDING (matches "record when Running" in the
# live app's simpler 5-state model, where ENDING is still nominally "Running"); evaluates the
# pattern on each new sample once ENDING (matches "pattern-check timer arms only once low power
# is first seen") rather than a separate 15s run_every loop - a deliberate, documented adaptation
# to the engine's simpler event model (spec 5.1 tick cadence is a generic 60s backstop, not a
# per-detector custom-interval loop); the DETECTION MATH (ring buffer stats + thresholds) is
# unchanged. Emits KEEP_FRESH once per cycle (mirrors the live app's own self-cancel-on-detect).
# ---------------------------------------------------------------------------------------------

class KeepFreshDetector(Detector):
    name = "keep_fresh"

    def __init__(self, pattern_window=10):
        self.pattern_window = pattern_window
        self._readings = []
        self._done = False

    def wire(self, ctx):
        ctx.subscribe(ctx.config["power_sensor"], self.on_state)

    def on_state(self, entity, old, new, ctx):
        if ctx.state not in (State.RUNNING, State.ENDING):
            self._readings = []
            self._done = False
            return
        try:
            watts = float(new)
        except (ValueError, TypeError):
            return
        self._readings.append(watts)
        if len(self._readings) > self.pattern_window:
            self._readings.pop(0)
        if ctx.state != State.ENDING or self._done or len(self._readings) < self.pattern_window:
            return
        mean_power = statistics.mean(self._readings)
        max_power = max(self._readings)
        min_power = min(self._readings)
        stdev = statistics.stdev(self._readings) if len(self._readings) > 1 else 0
        main_cycle_power = ctx.config["main_cycle_power"]
        has_zero_periods = min_power < 5
        has_moderate_spikes = 100 < max_power < main_cycle_power
        not_main_cycle = mean_power < main_cycle_power / 3
        main_cycle_done = (has_zero_periods and has_moderate_spikes and not_main_cycle) or (
            mean_power < 250 and max_power < main_cycle_power and stdev > 50
        )
        if main_cycle_done:
            self._done = True
            ctx.emit(Evidence.make(
                E.KEEP_FRESH, ctx.now(), self.name,
                payload={"mean": mean_power, "peak": max_power, "stdev": stdev},
            ))


# ---------------------------------------------------------------------------------------------
# DryerPolicy: stateful glue (physics attributes) + guards/actions/table for TransitionTable.
# ---------------------------------------------------------------------------------------------

class DryerPolicy:
    """The `policy` object ApplianceFSM is constructed with. Holds cycle physics as plain
    attributes (mirrors dryer_monitor.py's own self.* - the engine holds none of this, spec
    section 1), config (merged DEFAULTS + caller overrides), and the guards/actions/table triple
    the engine's TransitionTable validates and dispatches by name."""

    def __init__(self, config=None, profiles=None, learned=None):
        self.cfg = {**DEFAULTS, **(config or {})}
        self.profiles = profiles if profiles is not None else dict(_PROFILE_DEFAULTS)
        self.learned = learned if learned is not None else {}

        self.start_time: Optional[datetime] = None
        self.energy_start: Optional[float] = None
        self.detected_programme = "unknown"
        self._restored_detected_programme: Optional[str] = None
        self.expected_dur_at_start: Optional[float] = None
        self.max_power_w = 0.0
        self.last_high_energy_at: Optional[datetime] = None
        self.door_opened_time: Optional[datetime] = None
        self.door_opened_during_cycle = False
        self.keep_fresh_detected = False
        self.notification_sent = False
        self._ending_since: Optional[datetime] = None
        self._reconcile_handle = None
        # Persisted mirror of the engine's own in-memory exactly-once guard (spec section 6) - the
        # engine's _last_feedback_cycle_id does not survive a process restart on its own; this
        # round-trips through the store (_STORE_FIELDS below) so dryer_shadow.py can call
        # ctx-level fsm.mark_fed_back(...) at boot and pre-arm the guard for the cycle_id it is
        # restoring, closing a gap the live dryer_monitor.py's own _feedback_saved_cycle_id never
        # had to close (it is in-memory only there too, but no restore ever replays it).
        self.last_feedback_cycle_id: Optional[str] = None

        # Spec 4.2's four watchdogs. Held here (not in `detectors=`, WatchdogTimer's own docstring:
        # "does not wire or tick") - armed/cancelled from actions below and from
        # sync_watchdogs_on_publish (cancel-only, keyed on the landed state - see its docstring).
        # WatchdogTimer._fire() itself retries on a cooling-REFUSED fire (appliance_detectors.py,
        # upstream fix - was a local _RetryingWatchdogTimer subclass here before that landed).
        self.watchdogs = {
            "running": WatchdogTimer("running", self.cfg["max_running_hours"] * 3600),
            "pause": WatchdogTimer("pause", self.cfg["pause_timeout_minutes"] * 60),
            "unemptied": WatchdogTimer("unemptied", self.cfg["unemptied_timeout_hours"] * 3600),
            "emptied": WatchdogTimer("emptied", self.cfg["emptied_timeout_minutes"] * 60),
        }

        self.guards = {
            "G_power_high_live": self.g_power_high_live,
            "G_power_low_live": self.g_power_low_live,
            "G_power_high": self.g_power_high,
            "G_fill_window": self.g_fill_window,
            "G_valid_cycle": self.g_valid_cycle,
            "G_past_guard": self.g_past_guard,
            "G_is_real": self.g_is_real,
            "G_door_edge": self.g_door_edge,
            "G_reconcilable": self.g_reconcilable,
        }
        self.actions = {
            "a_start_confirmed": self.a_start_confirmed,
            "a_track_power_high": self.a_track_power_high,
            "a_enter_ending_power": self.a_enter_ending_power,
            "a_enter_ending_keep_fresh": self.a_enter_ending_keep_fresh,
            "a_door_high_pause": self.a_door_high_pause,
            "a_door_pause_track": self.a_door_pause_track,
            "a_resume_from_pause": self.a_resume_from_pause,
            "a_finish_silent": self.a_finish_silent,
            "a_finish_announce": self.a_finish_announce,
            "a_finish_skip_if_not_real": self.a_finish_skip_if_not_real,
            "a_door_emptying": self.a_door_emptying,
            "a_force_off": self.a_force_off,
            "a_plug_outage_wipe": self.a_plug_outage_wipe,
            "a_door_emptied_close": self.a_door_emptied_close,
            "a_force_emptied": self.a_force_emptied,
            "a_door_emptying_from_finished": self.a_door_emptying_from_finished,
            "a_reconcile": self.a_reconcile,
        }
        self.table = TABLE

    # ---- guards (spec 4.1) ----

    def g_power_high_live(self, ctx):
        return self._live_watts(ctx) >= self.cfg["start_w"]

    def g_power_low_live(self, ctx):
        return self._live_watts(ctx) <= self.cfg["stop_w"]

    def g_power_high(self, ctx):
        """Evidence-payload watts >= main-cycle floor OR the evidence isn't itself a low/keep-fresh
        signal (dryer_monitor.py:1432-1433,1468) - used to route a DOOR_OPENED while Running."""
        watts = (ctx.evidence.payload or {}).get("power_w", 0.0)
        return watts > self.cfg["stop_w"] and not self.keep_fresh_detected

    def g_fill_window(self, ctx):
        return self._run_minutes(ctx) < self.cfg["fill_window_minutes"]

    def g_valid_cycle(self, ctx):
        return is_valid_completed_cycle(
            self._run_minutes(ctx), self._energy_used(ctx), self.keep_fresh_detected,
            self.cfg["min_cycle_minutes"], self.cfg["min_energy_kwh"],
        )

    def g_past_guard(self, ctx):
        prog = self._effective_key(ctx)
        dur = guard_duration(self.profiles, self.learned, prog, self.expected_dur_at_start, self.cfg["max_running_hours"])
        return self._run_minutes(ctx) >= dur * 0.8

    def g_is_real(self, ctx):
        prog = self._classify(ctx)
        return get_profile(self.profiles, prog).get("is_real", True) is not False

    def _door_edge_anchor(self, ctx):
        """The instant to check door history AFTER. Two callers, two anchors: the normal
        ENDING-triggered finish path anchors to _ending_since (stamped when RUNNING->ENDING - see
        a_enter_ending_power/a_enter_ending_keep_fresh); the RECONCILE path has no _ending_since
        at all (a restore never runs those actions - enter_state_silently bypasses the table
        entirely) so it falls back to the same history-estimated finish time the boot self-heal
        hotfix uses (dryer_monitor.py:608-611's end_estimate), and finally to start_time itself if
        even that estimate is unavailable - matches this appliance's OWN documented behavior for
        the case this anchor exists to serve."""
        if self._ending_since is not None:
            return self._ending_since
        if self.start_time is None:
            return None
        hint = programme_duration(self.profiles, self.learned, self.detected_programme, use_learned=True) if self.detected_programme != "unknown" else None
        return estimate_cycle_end_from_history(
            ctx, self.cfg["energy_sensor"], self.start_time, self.cfg["min_cycle_minutes"], self.cfg["energy_active_watts"], expected_duration_min=hint
        ) or self.start_time

    def g_door_edge(self, ctx):
        """True when the door is open right now, or a recorder edge exists since the finish
        anchor (dryer_monitor.py's own new _door_open_edge_since, 1244-1274) - see
        _door_edge_anchor for which anchor applies to which caller."""
        if ctx.get_state(self.cfg["door_sensor"]) == "on":
            return True
        return door_open_edge_since(ctx, self.cfg["door_sensor"], self._door_edge_anchor(ctx))

    def g_reconcilable(self, ctx):
        """RECONCILE's own condition (washer_monitor.py:1276-1348, D2 invariant): the restore is
        still hypothesis, a real cycle length has already elapsed, and every recorded power sample
        over the last restore_corroboration_window_minutes stayed <= stop_w. Absent history is
        NEVER evidence of "off" - only a populated, all-low window concludes."""
        if not ctx.hypothesis or self.start_time is None:
            return False
        if self._run_minutes(ctx) < self.cfg["min_cycle_minutes"]:
            return False
        window_min = self.cfg["restore_corroboration_window_minutes"]
        hist = _flatten_history(
            ctx.get_history(self.cfg["power_sensor"], start_time=ctx.now() - timedelta(minutes=window_min), end_time=ctx.now()),
            self.cfg["power_sensor"],
        )
        points = []
        for entry in hist:
            s = entry.get("state")
            if s in (None, "unknown", "unavailable"):
                continue
            try:
                points.append(float(s))
            except (ValueError, TypeError):
                continue
        if not points:
            return False
        return all(w <= self.cfg["stop_w"] for w in points)

    # ---- guard helpers ----

    def _live_watts(self, ctx):
        v = ctx.get_state(self.cfg["power_sensor"])
        try:
            return float(v) if v not in (None, "unknown", "unavailable") else 0.0
        except (ValueError, TypeError):
            return 0.0

    def _run_minutes(self, ctx):
        if self.start_time is None:
            return 0.0
        return (ctx.now() - self.start_time).total_seconds() / 60

    def _energy_used(self, ctx):
        if self.energy_start is None:
            return 0.0
        v = ctx.get_state(self.cfg["energy_sensor"])
        try:
            return float(v) - self.energy_start if v not in (None, "unknown", "unavailable") else 0.0
        except (ValueError, TypeError):
            return 0.0

    def _classify(self, ctx):
        return classify_programme(self._run_minutes(ctx), self._energy_used(ctx))

    def _effective_key(self, ctx):
        return get_confirmed_programme_key(ctx) or self._classify(ctx)

    # ---- actions ----

    def a_start_confirmed(self, ctx):
        """STARTING/POWER_START_CONFIRMED -> RUNNING (dryer_monitor.py:2153-2216 shape, minus
        timer arming - PowerStartDetector's own confirm already proved this)."""
        watts = (ctx.evidence.payload or {}).get("watts", self.cfg["start_w"])
        ctx.mint_cycle_id()
        self.start_time = ctx.now()
        self.energy_start = self._read_energy(ctx)
        self.keep_fresh_detected = False
        self.notification_sent = False
        self.door_opened_during_cycle = False
        self.max_power_w = watts
        self.last_high_energy_at = ctx.now()
        prog = self._classify(ctx)
        self.detected_programme = prog
        self.expected_dur_at_start = guard_duration(self.profiles, self.learned, self._effective_key(ctx), None, self.cfg["max_running_hours"])
        self.watchdogs["running"].arm(ctx)
        ctx.log(f"State -> Running (programme guess {prog})", level="INFO")

    def _read_energy(self, ctx):
        v = ctx.get_state(self.cfg["energy_sensor"])
        try:
            return float(v) if v not in (None, "unknown", "unavailable") else None
        except (ValueError, TypeError):
            return None

    def a_track_power_high(self, ctx):
        """RUNNING/POWER_HIGH (dryer_monitor.py:1906-1909, high_power_counter dropped per F4). A
        live high sample also clears a lingering hypothesis (spec section 8: "a live POWER sample
        >= start_w clears the hypothesis and cancels the reconcile")."""
        watts = (ctx.evidence.payload or {}).get("watts", 0.0)
        self.max_power_w = max(self.max_power_w, watts)
        if watts > self.cfg["stop_w"]:
            self.last_high_energy_at = ctx.now()
        if ctx.hypothesis:
            ctx.cancel(self._reconcile_handle)
            self._reconcile_handle = None
            ctx.clear_hypothesis()
            ctx.log("Hypothesis cleared: live power sample confirms a genuinely running cycle", level="INFO")

    def a_enter_ending_power(self, ctx):
        """RUNNING/POWER_LOW -> ENDING (dryer_monitor.py:1943-1957 minus timer arm, PowerEndDetector
        owns that). Stamps the door-edge anchor (see g_door_edge)."""
        self._ending_since = ctx.now()

    def a_enter_ending_keep_fresh(self, ctx):
        """RUNNING/KEEP_FRESH -> ENDING (dryer_monitor.py:1988-1990 shape)."""
        self.keep_fresh_detected = True
        self._ending_since = ctx.now()

    def a_door_high_pause(self, ctx):
        """RUNNING/DOOR_OPENED with power high -> PAUSED (dryer_monitor.py:1468-1472). Running
        watchdog is deliberately left untouched (spans Running+Paused, spec 4.2) - only pause's
        own watchdog arms here."""
        self.door_opened_time = ctx.now()
        self.door_opened_during_cycle = True
        self.watchdogs["pause"].arm(ctx)

    def a_door_pause_track(self, ctx):
        """PAUSED/DOOR_OPENED (re-opened while already paused) - dryer_monitor.py:1474-1475."""
        self.door_opened_time = ctx.now()

    def a_resume_from_pause(self, ctx):
        """PAUSED/DOOR_CLOSED with power high -> RUNNING (dryer_monitor.py:1491-1494). The live
        power read that gated this row already proves the cycle is genuinely running."""
        self.door_opened_time = None
        if ctx.hypothesis:
            ctx.cancel(self._reconcile_handle)
            self._reconcile_handle = None
            ctx.clear_hypothesis()

    def _finish(self, ctx, *, skip_announce, end_reason, lands_on, force_push=False):
        """Shared 'declare finished' bundle for every table row that lands on FINISHED or EMPTIED -
        builds the feedback record, requests it (engine enforces exactly-once), announces/pushes
        subject to the hypothesis gate (engine-enforced) and the announce_entity toggle
        (dryer_monitor.py:1775-1851's shape, minus Running-progress attributes - the v2 entity
        contract, spec 7.3, does not carry those), and arms the ONE watchdog the target state
        owns (spec 4.2) - `lands_on` is "FINISHED" (arms unemptied) or "EMPTIED" (arms emptied
        directly, skipping unemptied entirely - the cycle never dwells in Unemptied on this route).
        cancel-side watchdog bookkeeping is centralized elsewhere (sync_watchdogs_on_publish)."""
        run_min = self._run_minutes(ctx)
        prog = self._classify(ctx)
        hint = programme_duration(self.profiles, self.learned, self.detected_programme, use_learned=True) if self.detected_programme != "unknown" else None
        run_minutes, duration_source = correct_duration(
            ctx, run_min, energy_sensor=self.cfg["energy_sensor"], start_time=self.start_time,
            min_cycle_minutes=self.cfg["min_cycle_minutes"], energy_active_watts=self.cfg["energy_active_watts"],
            duration_hint=hint,
        )
        idle_min = run_min - run_minutes if duration_source and run_min > run_minutes else None
        energy_kwh = self._energy_used(ctx)
        confirmed, is_human = get_confirmed_from_selector(ctx, prog)
        record = build_feedback_record(
            ts_local=ctx.now().isoformat(), predicted=prog, confirmed=confirmed, duration_min=run_minutes,
            energy_kwh=energy_kwh, max_power_w=self.max_power_w, programme_confirmed_by_human=is_human,
            duration_source=duration_source, end_reason=end_reason, idle_min=idle_min,
        )
        ctx.request_feedback(record)
        # ctx.cycle_id is stable across this call (request_feedback neither mints nor clears it);
        # stamped unconditionally, matching the engine's own guard, so the store always reflects
        # the most recently attempted cycle - see mark_fed_back's own docstring for why an
        # already-fed-back cycle_id here is exactly what a restore needs to replay.
        self.last_feedback_cycle_id = ctx.cycle_id
        if not skip_announce and not self.notification_sent:
            announce_entity = self.cfg.get("announce_entity")
            enabled = ctx.get_state(announce_entity) == "on" if announce_entity else True
            if enabled:
                if force_push:
                    ctx.push_mobile(f"Dryer finished about {run_min:.0f} min ago (late detection) - ready to empty.")
                else:
                    ctx.announce(self.cfg["announce_message"])
                self.notification_sent = True
        if lands_on == "FINISHED":
            self.watchdogs["unemptied"].arm(ctx)
        elif lands_on == "EMPTIED":
            self.watchdogs["emptied"].arm(ctx)

    def a_finish_announce(self, ctx):
        self._finish(ctx, skip_announce=False, end_reason="low_power_detected", lands_on="FINISHED")

    def a_finish_silent(self, ctx):
        self._finish(ctx, skip_announce=True, end_reason="door_opened_first", lands_on="EMPTIED")

    def a_finish_skip_if_not_real(self, ctx):
        skip = not self.g_is_real(ctx)
        end_reason = "keep_fresh_detected" if self.keep_fresh_detected else "low_power_detected"
        self._finish(ctx, skip_announce=skip, end_reason=end_reason, lands_on="FINISHED")

    def a_door_emptying(self, ctx):
        """RUNNING/DOOR_OPENED user-is-emptying combo row -> straight to EMPTIED (dryer_monitor.py:
        1448-1467): the finish bundle runs silently, target lands on EMPTIED directly (see TABLE)."""
        self._finish(ctx, skip_announce=True, end_reason="door_opened_first", lands_on="EMPTIED")

    def a_force_off(self, ctx):
        """Any WD_* -> OFF backstop (dryer_monitor.py:1545-1597,1553-1569). Watchdog cancel +
        physics reset for the OFF landing itself is centralized (sync_watchdogs_on_publish)."""
        ctx.log(f"Watchdog forced Off from evidence {ctx.evidence.type.name}", level="WARNING")

    def a_plug_outage_wipe(self, ctx):
        """RUNNING/PLUG_OUTAGE -> OFF (dryer_monitor.py:2231-2249's shape: sustained sensor outage
        wipes the in-progress cycle rather than leaving a phantom Running with no data - the wipe
        itself is sync_watchdogs_on_publish's OFF-landing reset, this just logs why)."""
        ctx.log("Plug outage sustained past grace - wiping in-progress cycle to Off", level="WARNING")

    def a_door_emptied_close(self, ctx):
        """EMPTIED/DOOR_CLOSED -> OFF (the 34h-wedge fix, dryer_monitor.py:1498-1506) - just a
        state landing; door_fast_start re-arm is DoorEdgeDetector/PowerStartDetector's own job."""

    def a_force_emptied(self, ctx):
        """FINISHED/FORCE_EMPTIED (dashboard button) -> EMPTIED (dryer_monitor.py:1853-1867). No
        new feedback record - the cycle already saved one becoming Unemptied; only the emptied
        watchdog is new here (unemptied's own cancel is sync_watchdogs_on_publish's job)."""
        ctx.log("Force Emptied via dashboard event", level="INFO")
        self.watchdogs["emptied"].arm(ctx)

    def a_door_emptying_from_finished(self, ctx):
        """FINISHED/DOOR_OPENED -> EMPTIED (dryer_monitor.py:1477-1479) - same shape as
        a_force_emptied: no new feedback record, just the emptied watchdog."""
        self.watchdogs["emptied"].arm(ctx)

    def a_reconcile(self, ctx):
        """RECONCILE landing action (RUNNING or PAUSED, both routes to this same bundle): the
        target (EMPTIED vs FINISHED) is decided by the table row's guard combo (g_door_edge), this
        only clears hypothesis and runs the finish bundle. force_push=True on the FINISHED route
        makes Sonos structurally impossible on a boot-time conclusion (washer_monitor.py:1340-1348's
        D2 invariant - simpler and stronger than the finish_anchor_override latency arithmetic
        spec section 8 sketches, so used directly; the deviation noted in this module's docstring).
        The EMPTIED route is always silent (no push at all - the door already answered).
        Cancels any still-pending scheduled handle too: try_conclude_at_boot can reach this
        synchronously, right after arm_reconcile already scheduled the 60s backstop for the same
        hypothesis - left uncancelled it would fire again later at a state with no RECONCILE row
        (a harmless DEBUG no-op, but a stray timer is still worth not leaving armed)."""
        ctx.clear_hypothesis()
        if self._reconcile_handle is not None:
            ctx.cancel(self._reconcile_handle)
        self._reconcile_handle = None
        if self.g_door_edge(ctx):
            self._finish(ctx, skip_announce=True, end_reason="boot_reconcile", lands_on="EMPTIED")
        else:
            self._finish(ctx, skip_announce=False, end_reason="boot_reconcile", force_push=True, lands_on="FINISHED")

    def arm_reconcile(self, ctx):
        """Schedule the one-shot RECONCILE 60s out (spec section 8). Called by dryer_shadow.py
        right after a boot restore lands in hypothesis=True, and re-armed by nothing else - a live
        power sample clears the hypothesis and cancels this (a_track_power_high/a_resume_from_pause)."""
        self._reconcile_handle = ctx.schedule(60, lambda: ctx.emit(Evidence.make(E.RECONCILE, ctx.now(), "reconcile", live=False)))

    def try_conclude_at_boot(self, ctx):
        """Eager boot-time self-heal check (spec section 4.3's closing bullet: 'route through the
        SAME FINISHED transition, but under the hypothesis rules') - emits RECONCILE immediately
        instead of waiting the full 60s, for the common case where the cycle was already long over
        BEFORE the restart. MUST go through ctx.emit (-> engine submit()), never call a_reconcile
        directly - an action alone only runs its side effects, the engine's submit() is what
        actually applies the state transition (moved-state bookkeeping lives there, not in the
        action). g_reconcilable inside the table row's guards decides whether anything happens;
        this is a no-op (and harmless) when hypothesis is already False or not yet reconcilable -
        the 60s scheduled RECONCILE (arm_reconcile) remains the backstop either way."""
        if ctx.hypothesis:
            ctx.emit(Evidence.make(E.RECONCILE, ctx.now(), "reconcile_boot", live=False))

    # ---- watchdog sync (spec 4.2) ----

    def _reset_cycle_physics(self):
        """Landing on OFF, by ANY route, always means no active cycle - called once, centrally,
        from sync_watchdogs_on_publish rather than duplicated per OFF-landing action."""
        self.start_time = None
        self.energy_start = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False
        self.keep_fresh_detected = False
        self.notification_sent = False
        self.max_power_w = 0.0
        self.detected_programme = "unknown"
        self.expected_dur_at_start = None

    def sync_watchdogs_on_publish(self, ctx, state):
        """Cancel-only watchdog sync, keyed purely on the LANDED internal state - called from
        dryer_shadow.py's publish callback on every transition the engine lands (store-only or
        not, submit()-driven or enter_state_silently at boot), so a future new table row can never
        forget to cancel a watchdog it is leaving behind the way a dozen scattered per-action calls
        could. Cancel is always safe (idempotent, and correct even on a fresh boot before anything
        is armed) - the ARM side is deliberately NOT here, since a fresh submit()-driven transition
        needs a full-duration arm (done in the action itself: a_start_confirmed/a_door_high_pause/
        _finish) while a boot restore needs arm_remaining (rearm_watchdogs_after_restore, called
        once by dryer_shadow.py right after enter_state_silently)."""
        w = self.watchdogs
        if state == State.RUNNING:
            w["pause"].cancel(ctx)
        elif state == State.FINISHED:
            w["running"].cancel(ctx)
            w["pause"].cancel(ctx)
        elif state == State.EMPTIED:
            w["running"].cancel(ctx)
            w["pause"].cancel(ctx)
            w["unemptied"].cancel(ctx)
        elif state == State.OFF:
            for wd in w.values():
                wd.cancel(ctx)
            self._reset_cycle_physics()

    def rearm_watchdogs_after_restore(self, ctx, state, since):
        """Restart re-arm (spec 4.2's own column): floored remaining time from `since` (state_since
        - falls back to ctx.now(), i.e. a full period, when the restore had none), never a fresh
        full-duration arm - dryer_shadow.py calls this once, right after enter_state_silently, for
        RUNNING/PAUSED/FINISHED/EMPTIED. RUNNING+PAUSED both re-arm "running" (spans both, spec
        4.2); PAUSED additionally re-arms "pause"."""
        since = since or ctx.now()
        if state in (State.RUNNING, State.PAUSED):
            self.watchdogs["running"].arm_remaining(ctx, since)
        if state == State.PAUSED:
            self.watchdogs["pause"].arm_remaining(ctx, since)
        elif state == State.FINISHED:
            self.watchdogs["unemptied"].arm_remaining(ctx, since)
        elif state == State.EMPTIED:
            self.watchdogs["emptied"].arm_remaining(ctx, since)

    # ---- store round-trip (dryer_shadow.py's only touch point on this state) ----
    # (disk_key, attr, is_datetime) - mirrors cycle_persistence.py's _cycle_store_field_map shape
    # (a declarative {key: field} spec) without depending on the mixin itself (spec 7.1).
    _STORE_FIELDS = (
        ("cycle_start_time", "start_time", True), ("energy_at_start", "energy_start", False),
        ("detected_programme", "detected_programme", False), ("programme_duration_min", "expected_dur_at_start", False),
        ("max_power_w", "max_power_w", False), ("last_high_energy_at", "last_high_energy_at", True),
        ("door_opened_during_cycle", "door_opened_during_cycle", False), ("door_opened_time", "door_opened_time", True),
        ("keep_fresh_detected", "keep_fresh_detected", False), ("notification_sent", "notification_sent", False),
        ("last_feedback_cycle_id", "last_feedback_cycle_id", False),
    )

    def to_store_fields(self):
        from cycle_store import format_utc

        out = {}
        for key, attr, is_dt in self._STORE_FIELDS:
            v = getattr(self, attr)
            out[key] = (format_utc(v) if v else None) if is_dt else v
        return out

    def restore_from(self, fields):
        from cycle_store import parse_utc

        fields = fields or {}
        for key, attr, is_dt in self._STORE_FIELDS:
            if key not in fields or fields[key] in (None, ""):
                continue
            setattr(self, attr, parse_utc(fields[key]) if is_dt else fields[key])
        if fields.get("detected_programme"):
            self._restored_detected_programme = fields["detected_programme"]


# ---------------------------------------------------------------------------------------------
# Boot-restore precedence (spec 4.3) + corroboration (spec section 8) - pure functions of already
# -fetched inputs; dryer_shadow.py does the I/O (get_state/store.load/helper read), these decide.
# ---------------------------------------------------------------------------------------------

VALID_STATES = ("Running", "Unemptied", "Paused", "Emptied")


def resolve_boot_snapshot(*, entity_state, entity_attrs, entity_last_changed, store_data, helper_state,
                           now, cfg):
    """dryer_monitor.py:292-428's precedence (entity -> store -> helper -> Off), minus the
    AppDaemon plumbing: entity/store staleness+future-clock rejection reuses the same numeric
    rules as cycle_persistence.py's _cycle_store_validate_running_candidate (F5; not imported -
    that method lives on the mixin, which dryer_shadow.py deliberately does not subclass, spec
    7.1 - reimplemented here as a pure function of already-fetched values instead).

    Returns a plain dict shaped like BOOT_RESTORE's payload convention (appliance_fsm.py's Evidence
    docstring): {"state","source","start_time"(attr dict; caller merges into policy via restore_from),
    "state_since","cycle_id","code_fingerprint","store_fields"} or None for a fresh boot (Off, no
    restore needed at all)."""
    entity_sourced = entity_state in VALID_STATES
    if entity_sourced:
        state_since = entity_last_changed
        return {
            "state": entity_state, "source": "entity", "state_since": state_since,
            "cycle_id": (store_data or {}).get("cycle_id"), "code_fingerprint": (store_data or {}).get("code_fingerprint"),
            "store_fields": store_data or {},
        }

    store_state = (store_data or {}).get("state")
    store_ok = store_state in VALID_STATES
    if store_ok and store_state in ("Running", "Paused"):
        start_time = _parse_hist_ts((store_data or {}).get("cycle_start_time"))
        saved_at = _parse_hist_ts((store_data or {}).get("saved_at"))
        store_ok = _validate_running_candidate(
            start_time=start_time, saved_at=saved_at, now=now,
            future_skew_s=cfg["boot_future_start_skew_s"], max_running_hours=cfg["max_running_hours"],
            max_downtime_hours=cfg["store_max_downtime_hours"],
        )
    if store_ok:
        return {
            "state": store_state, "source": "store", "state_since": (store_data or {}).get("state_since"),
            "cycle_id": (store_data or {}).get("cycle_id"), "code_fingerprint": (store_data or {}).get("code_fingerprint"),
            "store_fields": store_data or {},
        }

    if helper_state in ("Running", "Paused"):
        return None  # bare helper never carries a clock - falls through to Off (dryer_monitor.py:374-386)
    if helper_state in VALID_STATES:
        return {"state": helper_state, "source": "helper", "state_since": None, "cycle_id": None, "code_fingerprint": None, "store_fields": {}}
    return None


def _validate_running_candidate(*, start_time, saved_at, now, future_skew_s, max_running_hours, max_downtime_hours):
    """cycle_persistence.py:177-232's rule set, reimplemented (see resolve_boot_snapshot's
    docstring for why this is not a mixin call)."""
    if start_time is None:
        return False
    skew_s = (start_time - now).total_seconds()
    if skew_s > future_skew_s:
        return False
    if (now - start_time).total_seconds() / 3600 > max_running_hours:
        return False
    if saved_at is not None and (now - saved_at).total_seconds() / 3600 > max_downtime_hours:
        return False
    return True


def corroborate_restore(*, boot_watts, last_high_energy_at, now, cfg, code_fingerprint, stored_fingerprint):
    """spec section 8: corroborated = (boot_watts > stop_w OR last_high_energy_at within
    restore_corroboration_window_minutes) AND NOT a code_fingerprint mismatch. dryer reuses stop_w
    as the standby ceiling (no new knob, per spec). Returns (hypothesis: bool, why: str)."""
    recent_high = last_high_energy_at is not None and (now - last_high_energy_at) <= timedelta(
        minutes=cfg["restore_corroboration_window_minutes"]
    )
    power_ok = boot_watts > cfg["stop_w"]
    mismatch = stored_fingerprint is not None and code_fingerprint is not None and stored_fingerprint != code_fingerprint
    if (power_ok or recent_high) and not mismatch:
        return False, "corroborated"
    why = "store code fingerprint changed" if mismatch else (
        f"power {boot_watts:.0f}W <= {cfg['stop_w']:.0f}W and no high-energy sample within "
        f"{cfg['restore_corroboration_window_minutes']:.0f}min"
    )
    return True, why


# ---------------------------------------------------------------------------------------------
# The transition table (spec section 4). Rows whose spec Action is purely detector-owned
# (arm/cancel a confirm timer, arm/clear the door-fast-start window) carry no `action` here - see
# this module's docstring. RECONCILE rows are engine-native additions not in dryer_monitor.py at
# all (spec section 8) - see the docstring's note on the one spec deviation (force_push).
# ---------------------------------------------------------------------------------------------

TABLE = {
    S.OFF: {
        E.POWER_HIGH: [Row(target=S.STARTING)],
        E.POWER_LOW: [Row()],
        E.DOOR_OPENED: [Row()],
        E.DOOR_CLOSED: [Row()],
    },
    S.STARTING: {
        E.POWER_START_CONFIRMED: [Row(guards=("G_power_high_live",), action="a_start_confirmed", target=S.RUNNING)],
        E.POWER_LOW: [Row(target=S.OFF)],
        E.DOOR_OPENED: [Row()],
    },
    S.RUNNING: {
        E.POWER_LOW: [Row(action="a_enter_ending_power", target=S.ENDING)],
        E.KEEP_FRESH: [Row(action="a_enter_ending_keep_fresh", target=S.ENDING)],
        E.POWER_HIGH: [Row(action="a_track_power_high")],
        E.DOOR_OPENED: [
            Row(guards=("G_power_high",), action="a_door_high_pause", target=S.PAUSED, cooling=RESPECT),
            Row(guards=("G_fill_window",), target=S.OFF, cooling=BYPASS),
            Row(guards=("G_valid_cycle", "G_past_guard"), action="a_door_emptying", target=S.EMPTIED, cooling=BYPASS),
            # Catch-all: NOT fill_window, and (NOT valid_cycle OR NOT past_guard) - spec's two
            # separate OFF rows (:1438-1439,:1444-1446) collapse to one here (identical action/
            # target - no-op, land on OFF) since row order already excludes the prior guard combos.
            Row(target=S.OFF, cooling=BYPASS),
        ],
        E.WD_RUNNING: [Row(action="a_force_off", target=S.OFF, cooling=RESPECT)],
        E.PLUG_OUTAGE: [Row(action="a_plug_outage_wipe", target=S.OFF)],
        E.RECONCILE: [
            Row(guards=("G_reconcilable", "G_door_edge"), action="a_reconcile", target=S.EMPTIED),
            Row(guards=("G_reconcilable",), action="a_reconcile", target=S.FINISHED),
        ],
    },
    S.PAUSED: {
        E.DOOR_CLOSED: [
            Row(guards=("G_power_high_live",), action="a_resume_from_pause", target=S.RUNNING, cooling=BYPASS),
            Row(guards=("G_power_low_live", "G_valid_cycle"), action="a_finish_announce", target=S.FINISHED, cooling=BYPASS),
            Row(guards=("G_power_low_live",), target=S.OFF, cooling=BYPASS),
        ],
        E.WD_PAUSE: [Row(target=S.OFF, cooling=BYPASS)],
        E.WD_RUNNING: [Row(action="a_force_off", target=S.OFF, cooling=RESPECT)],
        E.DOOR_OPENED: [Row(action="a_door_pause_track")],
        E.RECONCILE: [
            Row(guards=("G_reconcilable", "G_door_edge"), action="a_reconcile", target=S.EMPTIED),
            Row(guards=("G_reconcilable",), action="a_reconcile", target=S.FINISHED),
        ],
    },
    S.ENDING: {
        E.POWER_END_CONFIRMED: [
            Row(guards=("G_past_guard", "G_valid_cycle", "G_door_edge"), action="a_finish_silent", target=S.EMPTIED, cooling=BYPASS),
            Row(guards=("G_past_guard", "G_valid_cycle"), action="a_finish_skip_if_not_real", target=S.FINISHED, cooling=RESPECT),
            Row(target=S.RUNNING),
        ],
        E.KEEP_FRESH: [Row(guards=("G_past_guard", "G_valid_cycle", "G_is_real"), action="a_finish_announce", target=S.FINISHED, cooling=RESPECT)],
        E.POWER_RECOVERED: [Row(target=S.RUNNING)],
        E.DOOR_OPENED: [
            Row(guards=("G_power_high",), action="a_door_high_pause", target=S.PAUSED, cooling=RESPECT),
            Row(guards=("G_fill_window",), target=S.OFF, cooling=BYPASS),
            Row(guards=("G_valid_cycle", "G_past_guard"), action="a_door_emptying", target=S.EMPTIED, cooling=BYPASS),
            Row(target=S.OFF, cooling=BYPASS),
        ],
        E.WD_RUNNING: [Row(action="a_force_off", target=S.OFF, cooling=RESPECT)],
    },
    S.FINISHED: {
        E.DOOR_OPENED: [Row(action="a_door_emptying_from_finished", target=S.EMPTIED, cooling=BYPASS)],
        E.FORCE_EMPTIED: [Row(action="a_force_emptied", target=S.EMPTIED, cooling=BYPASS)],
        E.WD_UNEMPTIED: [Row(target=S.OFF, cooling=BYPASS)],
        E.POWER_HIGH: [Row()],
    },
    S.EMPTIED: {
        E.DOOR_CLOSED: [Row(action="a_door_emptied_close", target=S.OFF, cooling=BYPASS)],
        E.WD_EMPTIED: [Row(target=S.OFF, cooling=BYPASS)],
    },
}
