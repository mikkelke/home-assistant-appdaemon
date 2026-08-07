"""
Cycle classification and temperature/spin value coding for the washer monitor.

Two separate questions live here, both pure:

  * "Which programme was that?" - the centroid nearest-match against historical
    confirmed cycles (classify_from_history).
  * "Was that a real, learnable cycle?" - the duration/energy guard set that
    decides completion_class and valid_for_learning (classify_cycle_completion).
    Finish detection decides the UI state; this only classifies the saved record.

Plus the small value codecs that translate between what HA's input_selects hold
('40°C', '1400 rpm') and what the feedback JSON stores ('40', 1400).

washer_monitor.py keeps the thin methods that read the live entities and the
tuned thresholds off `self` and pass them in.

Sibling-import module (AppDaemon puts app dirs on sys.path), matching the
existing `import climate_model as cm` precedent in apps/climate/.
"""

import re

# Transition paths a record's end_reason may legitimately carry. Anything else in
# an old record is normalized to low_power_detected during migration.
KNOWN_TRANSITION_PATHS = (
    "user_cycle_end",
    "anti_crease_pattern",
    "low_power_detected",
    "door_opened_first",
    "tail_to_standby",
    "tail_pattern_break",
)

# Values an input_select shows when no choice has been made.
UNSET_SELECTOR_VALUES = ("—", "–", "-")


def log_safe(s):
    """Return string safe for log output (avoids encoding errors with ° in some environments)."""
    if s is None:
        return ""
    return str(s).replace("°", " ").replace("Â°", " ")


def temp_for_storage(temp):
    """Return temperature for JSON storage (no degree symbol). '40°C' -> '40'; 'cold' -> 'cold'; None/empty -> None."""
    if not temp or not isinstance(temp, str):
        return None
    s = temp.strip()
    if s.lower() == "cold":
        return "cold"
    s = s.replace("°C", "").replace("°", "").strip()
    return s if s else None


def temp_from_storage(s):
    """Return temperature for internal use (e.g. profile lookup). '40' or '40°C' -> '40°C'; 'cold' -> 'cold'; None/empty -> None."""
    if s is None or (isinstance(s, str) and not s.strip()):
        return None
    s = str(s).strip()
    if s.lower() == "cold":
        return "cold"
    if "°" in s:
        return s
    return f"{s}°C" if s else None


def normalize_selector_temperature(val):
    """Normalize a raw temperature input_select state to '40°C' / 'cold' / None.

    "—" (em dash) = no choice made; "Cold" = cold/snowflake (no heating).
    """
    if val.strip() in UNSET_SELECTOR_VALUES:
        return None
    if val.strip().lower() == "cold":
        return "cold"
    return val if "°" in val else f"{val}°C"


def parse_spin_rpm(value: str):
    """Parse spin speed from input_select state. Returns rpm (0 = no spin) or None."""
    if not value or value.strip() in ("—", ""):
        return None
    value = value.strip().lower()
    if "no spin" in value or "flydeslut" in value:
        return 0
    m = re.search(r"(\d+)\s*rpm", value)
    if m:
        return int(m.group(1))
    if value.isdigit():
        return int(value)
    return None


def classify_from_history(centroids, run_min: float, energy_used: float, heating_bursts: int):
    """Use power-pattern match to historical confirmed cycles when ambiguous.

    Centroids are keyed by "prog|temp" strings. Returns the winning key string, or
    None when there is no history, the run is too short, or nothing matched within
    the distance cut-off. The caller splits the key and drops a temperature that the
    programme does not actually carry.
    """
    if not centroids or run_min < 5:
        return None
    rate_curr = energy_used / run_min
    best_key = None
    best_dist = float("inf")
    for key, c in centroids.items():
        rate_dist = 500 * abs(rate_curr - c["rate"])
        burst_dist = 0.5 * abs(heating_bursts - c["heating_bursts"])
        dist = rate_dist + burst_dist
        if dist < best_dist:
            best_dist = dist
            best_key = key
    if best_key is None or best_dist > 2.0:
        return None
    return best_key


def classify_cycle_completion(
    run_minutes: float,
    energy_kwh: float,
    confirmed: str,
    transition_path: str,  # user_cycle_end | anti_crease_pattern | low_power_detected | door_opened_first
    profile: dict,
    user_conf: bool,
    guard_fraction: float,
    min_cycle_minutes: float,
    min_energy_kwh: float,
    validation_key: str,
):
    """Classify a completed cycle for learning quality. Returns completion_class,
    valid_for_learning, validation_flags, end_reason.

    `profile` is the resolved profile for the confirmed programme + temperature,
    `guard_fraction` is the caller's choice between the plain and user-confirmed
    completion guard fractions, and `validation_key` is the already-built
    "prog|temp" (or bare "prog") key stored on the record.
    """
    flags = []
    nominal_dur = profile.get("duration_min") or profile.get("nominal_duration_min") or 180
    max_dur = profile.get("max_dur_min") or profile.get("max_valid_duration_min") or int(nominal_dur * 1.2)
    frac = guard_fraction
    min_valid_dur = max(frac * nominal_dur, min_cycle_minutes)
    min_energy = profile.get("min_valid_energy_kwh") or min_energy_kwh
    if min_energy is None:
        min_energy = min_energy_kwh
    max_energy = profile.get("max_valid_energy_kwh") or profile.get("max_energy_kwh") or 3.0

    # A. Duration too short
    if run_minutes < min_valid_dur:
        flags.append("runtime_too_short")
    if run_minutes > max_dur:
        flags.append("duration_too_long")
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

    if "runtime_too_short" in flags and run_minutes < min_cycle_minutes:
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

    # D. Duration too long (e.g. a delayed-start wait that slipped past the trim guards) -
    # never trust it for learning, even if it otherwise looked "completed".
    if "duration_too_long" in flags and completion_class != "interrupted":
        completion_class = "suspect"

    valid_for_learning = (
        completion_class == "completed"
        and "runtime_too_short" not in flags
        and "energy_too_low" not in flags
        and "unknown_programme" not in flags
    )
    if transition_path == "door_opened_first" and completion_class != "completed":
        valid_for_learning = False
    if "duration_too_long" in flags:
        valid_for_learning = False

    return {
        "completion_class": completion_class,
        "valid_for_learning": valid_for_learning,
        "validation_flags": flags,
        "end_reason": transition_path,
        "programme_key_used_for_validation": validation_key,
    }
