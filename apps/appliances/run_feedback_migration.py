#!/usr/bin/env python3
"""
One-off script to apply washer feedback migration (add completion_class, valid_for_learning, etc.).
Run from repo root: python3 appdaemon/apps/appliances/run_feedback_migration.py
Uses same logic as WasherMonitor._migrate_feedback_add_completion_class (dry_run=False).
"""
import json
import os
import sys

# Paths relative to this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDBACK_FILE = os.path.join(SCRIPT_DIR, "washer_feedback.json")
PROGRAMMES_FILE = os.path.join(SCRIPT_DIR, "washer_programmes.yaml")

# Default profiles (minimal for migration; matches _DEFAULT_PROFILES for key programmes)
def _default_profiles():
    return {
        "bomuld": {"by_temperature": {
            "cold": {"duration_min": 159, "max_energy_kwh": 0.35},
            "20°C": {"duration_min": 159, "max_energy_kwh": 0.42},
            "30°C": {"duration_min": 159, "max_energy_kwh": 0.42},
            "40°C": {"duration_min": 175, "max_energy_kwh": 0.90},
            "60°C": {"duration_min": 149, "max_energy_kwh": 1.40},
            "90°C": {"duration_min": 160, "max_energy_kwh": 2.20},
        }},
        "uld": {"by_temperature": {
            "cold": {"duration_min": 39, "max_energy_kwh": 0.22},
            "30°C": {"duration_min": 39, "max_energy_kwh": 0.28},
            "40°C": {"duration_min": 39, "max_energy_kwh": 0.28},
        }, "max_dur_min": 55},
        "strygelet": {"by_temperature": {
            "cold": {"duration_min": 119, "max_energy_kwh": 0.35},
            "30°C": {"duration_min": 119, "max_energy_kwh": 0.52},
            "60°C": {"duration_min": 119, "max_energy_kwh": 0.52},
        }, "max_dur_min": 140},
        "finvask": {"by_temperature": {
            "cold": {"duration_min": 65, "max_energy_kwh": 0.25},
            "40°C": {"duration_min": 65, "max_energy_kwh": 0.40},
        }, "max_dur_min": 90},
        "eco": {"duration_min": 199, "max_dur_min": 235, "max_energy_kwh": 0.78},
        "ekspres": {"duration_min": 20, "max_dur_min": 30, "max_energy_kwh": 0.40},
        "morkt_denim": {"by_temperature": {"cold": {"duration_min": 90, "max_energy_kwh": 0.35}, "60°C": {"duration_min": 90, "max_energy_kwh": 0.55}}, "max_dur_min": 110},
        "outdoor": {"by_temperature": {"cold": {"duration_min": 50, "max_energy_kwh": 0.25}, "40°C": {"duration_min": 50, "max_energy_kwh": 0.40}}, "max_dur_min": 65},
        "impraegnering": {"duration_min": 25, "max_dur_min": 35, "max_energy_kwh": 0.15},
        "pumpe_centrifugering": {"duration_min": 10, "max_dur_min": 20, "max_energy_kwh": 0.08},
        "kun_skyl_stivelse": {"duration_min": 30, "max_dur_min": 45, "max_energy_kwh": 0.12},
        "unknown": {"duration_min": 180, "max_dur_min": 240, "max_energy_kwh": 2.50},
    }


def _load_yaml_programmes():
    try:
        import yaml
        with open(PROGRAMMES_FILE, "r") as f:
            data = yaml.safe_load(f) or {}
        return data.get("programmes", {})
    except Exception:
        return {}


def _merge_profiles(defaults, yaml_progs):
    """Merge YAML into defaults (YAML overrides)."""
    import copy
    merged = copy.deepcopy(defaults)
    for key, val in (yaml_progs or {}).items():
        if not isinstance(val, dict):
            continue
        if key in merged and isinstance(merged[key], dict) and "by_temperature" in merged.get(key, {}):
            for k, v in val.items():
                if k != "by_temperature":
                    merged[key][k] = v
        else:
            merged[key] = {**merged.get(key, {}), **val}
    return merged


def _temp_from_storage(t):
    if t is None or t == "" or (isinstance(t, str) and t.lower() in ("null", "none")):
        return None
    s = str(t).strip()
    if s in ("", "null", "none"):
        return None
    # Normalize 40 -> 40°C for profile lookup
    if s.isdigit():
        return f"{s}°C"
    return s if s else None


def _programme_has_temperature(prog):
    return prog in ("bomuld", "strygelet", "eco", "finvask", "ekspres", "morkt_denim", "outdoor")


def _get_profile(profiles, programme, temperature=None):
    prof = profiles.get(programme, profiles.get("unknown", {}))
    if "by_temperature" in prof:
        temps = prof["by_temperature"]
        if temperature and temperature in temps:
            p = dict(temps[temperature])
        else:
            p = dict(next(iter(temps.values())))
        p.setdefault("label", prof.get("label", programme))
        for k, v in prof.items():
            if k != "by_temperature" and k not in p:
                p[k] = v
        return p
    return prof


def classify_cycle_completion(rec, profiles, min_cycle_minutes=25, min_energy_kwh=0.1,
                              completion_guard_fraction=0.65, completion_guard_fraction_user_confirmed=0.60):
    """Same logic as WasherMonitor._classify_cycle_completion for migration."""
    confirmed = rec.get("confirmed", "")
    conf_temp = _temp_from_storage(rec.get("confirmed_temperature"))
    dur = rec.get("duration_min")
    if not isinstance(dur, (int, float)):
        return None
    run_minutes = float(dur)
    energy_kwh = float(rec.get("energy_kwh", 0) or 0)
    user_conf = rec.get("programme_user_confirmed", rec.get("user_confirmed", False))
    transition_path = rec.get("end_reason") or rec.get("transition_path") or "low_power_detected"
    if transition_path not in ("user_cycle_end", "anti_crease_pattern", "low_power_detected", "door_opened_first"):
        transition_path = "low_power_detected"

    profile = _get_profile(profiles, confirmed, conf_temp)
    nominal_dur = profile.get("duration_min") or profile.get("nominal_duration_min") or 180
    max_dur = profile.get("max_dur_min") or profile.get("max_valid_duration_min") or int(nominal_dur * 1.2)
    frac = completion_guard_fraction_user_confirmed if user_conf else completion_guard_fraction
    min_valid_dur = max(frac * nominal_dur, min_cycle_minutes)
    min_energy = profile.get("min_valid_energy_kwh", min_energy_kwh)
    max_energy = profile.get("max_valid_energy_kwh") or profile.get("max_energy_kwh") or 3.0

    flags = []
    if run_minutes < min_valid_dur:
        flags.append("runtime_too_short")
    if energy_kwh < min_energy:
        flags.append("energy_too_low")
    if energy_kwh > max_energy:
        flags.append("energy_too_high")
    if transition_path == "door_opened_first":
        flags.append("door_opened_first")
    if confirmed in ("unknown", "") or not confirmed:
        flags.append("unknown_programme")

    if "runtime_too_short" in flags and run_minutes < min_cycle_minutes:
        completion_class = "interrupted"
    elif "runtime_too_short" in flags or "energy_too_low" in flags or "energy_too_high" in flags:
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

    prog_key = f"{confirmed}|{conf_temp}" if (conf_temp and _programme_has_temperature(confirmed)) else confirmed
    return {
        "completion_class": completion_class,
        "valid_for_learning": valid_for_learning,
        "validation_flags": flags,
        "end_reason": transition_path,
        "programme_key_used_for_validation": prog_key,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    if not os.path.exists(FEEDBACK_FILE):
        print(f"Feedback file not found: {FEEDBACK_FILE}")
        sys.exit(1)

    with open(FEEDBACK_FILE, "r") as f:
        data = json.load(f)

    defaults = _default_profiles()
    yaml_progs = _load_yaml_programmes()
    profiles = _merge_profiles(defaults, yaml_progs)

    profile_version = "1"
    validation_version = "1"
    counts = {"completed": 0, "interrupted": 0, "suspect": 0, "learnable": 0, "quarantined": 0, "unchanged": 0}

    for rec in data.get("cycles", []):
        if rec.get("completion_class") and rec.get("valid_for_learning") is not None:
            if rec.get("profile_version") == profile_version and rec.get("validation_version") == validation_version:
                counts["unchanged"] += 1
                continue
        dur = rec.get("duration_min")
        if not isinstance(dur, (int, float)):
            continue
        classification = classify_cycle_completion(rec, profiles)
        if not classification:
            continue
        rec["completion_class"] = classification["completion_class"]
        rec["valid_for_learning"] = classification["valid_for_learning"]
        rec["validation_flags"] = classification["validation_flags"]
        rec["transition_path"] = classification["end_reason"]
        rec["programme_key_used_for_validation"] = classification["programme_key_used_for_validation"]
        rec["profile_version"] = profile_version
        rec["validation_version"] = validation_version
        rec["detected_at"] = rec.get("ts", "")
        counts[classification["completion_class"]] = counts.get(classification["completion_class"], 0) + 1
        if classification["valid_for_learning"]:
            counts["learnable"] += 1
        else:
            counts["quarantined"] += 1

    print(f"Migration {'dry-run ' if dry_run else ''}counts: completed={counts.get('completed', 0)} "
          f"interrupted={counts.get('interrupted', 0)} suspect={counts.get('suspect', 0)} "
          f"learnable={counts['learnable']} quarantined={counts['quarantined']} unchanged={counts['unchanged']}")

    if dry_run:
        print("Dry-run: no file written.")
        return

    data["migration_version"] = "1"
    with open(FEEDBACK_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Written: {FEEDBACK_FILE}")


if __name__ == "__main__":
    main()
