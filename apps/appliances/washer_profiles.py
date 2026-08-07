"""
Programme profile tables and pure profile lookups for the Miele WEA 035 washer.

Split out of washer_monitor.py: this module holds only data and pure functions.
No AppDaemon coupling, no `self`, no I/O - washer_monitor.py keeps the thin
methods that read `self.PROGRAMME_PROFILES` and delegate here.

Sibling-import module (AppDaemon puts the app directory on sys.path), matching
the existing `import climate_model as cm` precedent in apps/climate/.
"""

import copy

# Programme profiles loaded from washer_programmes.yaml at startup.
# Programme and temperature are independent dimensions.  For "bomuld",
# the profile depends on the selected temperature (by_temperature dict).
# UI matrix: allowed_temperatures, allowed_spin_speeds, default_temperature, default_spin, available_options (canonical values only).
PROGRAMME_DISPLAY_ORDER = [
    "ekspres", "uld", "bomuld", "finvask", "strygelet", "eco",
    "morkt_denim", "outdoor", "impraegnering", "pumpe_centrifugering", "kun_skyl_stivelse",
]
CANONICAL_SPIN = ["1400 rpm", "1200 rpm", "900 rpm", "700 rpm", "No spin"]
SPIN_MAX_1200 = ["1200 rpm", "900 rpm", "700 rpm", "No spin"]   # Manual: Strygelet, Uld, Ekspres, Mørkt/Denim, Imprægnering
SPIN_MAX_900 = ["900 rpm", "700 rpm", "No spin"]                 # Manual: Finvask, Outdoor
DEFAULT_PROFILES = {
    "ekspres":   {"label": "Ekspres",   "default_temp": None,   "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": SPIN_MAX_1200, "default_temperature": "40°C", "default_spin": "1200 rpm", "available_options": ["short"], "by_temperature": {
        "cold": {"duration_min": 20, "max_energy_kwh": 0.25, "heats": False, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
        "20°C": {"duration_min": 20, "max_energy_kwh": 0.40, "heats":  True, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
        "30°C": {"duration_min": 20, "max_energy_kwh": 0.40, "heats":  True, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
        "40°C": {"duration_min": 20, "max_energy_kwh": 0.40, "heats":  True, "stable_min":  8, "max_dur_min": 30, "supports_anti_crease": True},
    }},
    "uld":       {"label": "Uld",       "default_temp": "30°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": SPIN_MAX_1200, "default_temperature": "30°C", "default_spin": "1200 rpm", "available_options": [], "by_temperature": {
        "cold": {"duration_min":  39, "max_energy_kwh": 0.22, "heats": False, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
        "20°C": {"duration_min":  39, "max_energy_kwh": 0.26, "heats":  True, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
        "30°C": {"duration_min":  39, "max_energy_kwh": 0.28, "heats":  True, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
        "40°C": {"duration_min":  39, "max_energy_kwh": 0.28, "heats":  True, "stable_min": 10, "max_dur_min":  55, "supports_anti_crease": False},
    }},  # Manual: Uld 40°C -> cold; max spin 1200 rpm
    "bomuld":    {"label": "Bomuld",    "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C", "90°C"], "allowed_spin_speeds": CANONICAL_SPIN, "default_temperature": "40°C", "default_spin": "1400 rpm", "available_options": ["water_plus", "soak", "prewash"], "by_temperature": {
        "cold": {"duration_min": 159, "max_energy_kwh": 0.35, "heats": False, "stable_min": 10, "max_dur_min": 185, "supports_anti_crease": True},
        "20°C": {"duration_min": 159, "max_energy_kwh": 0.55, "heats":  True, "stable_min": 15, "max_dur_min": 185, "supports_anti_crease": True},
        "30°C": {"duration_min": 159, "max_energy_kwh": 0.75, "heats":  True, "stable_min": 15, "max_dur_min": 185, "supports_anti_crease": True},
        "40°C": {"duration_min": 175, "max_energy_kwh": 0.90, "heats":  True, "stable_min": 15, "max_dur_min": 210, "supports_anti_crease": True},
        "60°C": {"duration_min": 149, "max_energy_kwh": 1.40, "heats":  True, "stable_min": 15, "max_dur_min": 175, "supports_anti_crease": True},
        "90°C": {"duration_min": 160, "max_energy_kwh": 2.20, "heats":  True, "stable_min": 15, "max_dur_min": 195, "supports_anti_crease": True},
    }},
    "finvask":   {"label": "Finvask",   "default_temp": "40°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": SPIN_MAX_900, "default_temperature": "40°C", "default_spin": "900 rpm", "available_options": ["water_plus", "soak"], "by_temperature": {
        "cold": {"duration_min":  65, "max_energy_kwh": 0.25, "heats": False, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
        "20°C": {"duration_min":  65, "max_energy_kwh": 0.35, "heats":  True, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
        "30°C": {"duration_min":  65, "max_energy_kwh": 0.38, "heats":  True, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
        "40°C": {"duration_min":  65, "max_energy_kwh": 0.40, "heats":  True, "stable_min": 10, "max_dur_min":  90, "supports_anti_crease": True},
    }},  # Manual: 40°C -> cold; allows all lower temps (Cold, 20, 30, 40)
    "strygelet": {"label": "Strygelet", "default_temp": "30°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C"], "allowed_spin_speeds": SPIN_MAX_1200, "default_temperature": "30°C", "default_spin": "1200 rpm", "available_options": ["water_plus", "soak"], "by_temperature": {
        "cold": {"duration_min": 119, "max_energy_kwh": 0.35, "heats": False, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
        "20°C": {"duration_min": 119, "max_energy_kwh": 0.45, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
        "30°C": {"duration_min": 119, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
        "40°C": {"duration_min": 119, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
        "60°C": {"duration_min": 119, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 15, "max_dur_min": 140, "supports_anti_crease": True},
    }},  # Manual: 60°C -> cold; intermediate temps 20–40
    "eco":       {"label": "ECO",       "default_temp": "40-60°C", "allowed_temperatures": ["40-60°C", "40°C", "60°C"], "default_temperature": "40-60°C", "allowed_spin_speeds": CANONICAL_SPIN, "default_spin": "1400 rpm", "available_options": ["water_plus", "soak"], "by_temperature": {
        # All three variants seeded identically (Manual p62 at 7 kg: 3:19 = 199 min, for the
        # auto 40-60°C range specifically) - no per-fixed-temperature breakdown exists yet;
        # learning differentiates them from confirmed cycles going forward.
        "40-60°C": {"duration_min": 199, "max_energy_kwh": 0.78, "heats":  True, "stable_min": 15, "max_dur_min": 235, "supports_anti_crease": True},
        "40°C": {"duration_min": 199, "max_energy_kwh": 0.78, "heats":  True, "stable_min": 15, "max_dur_min": 235, "supports_anti_crease": True},
        "60°C": {"duration_min": 199, "max_energy_kwh": 0.78, "heats":  True, "stable_min": 15, "max_dur_min": 235, "supports_anti_crease": True},
    }},
    "morkt_denim":  {"label": "Mørkt/Denim", "default_temp": "60°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C"], "allowed_spin_speeds": SPIN_MAX_1200, "default_temperature": "60°C", "default_spin": "1200 rpm", "available_options": ["water_plus"], "by_temperature": {
        "cold": {"duration_min":  90, "max_energy_kwh": 0.35, "heats": False, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
        "20°C": {"duration_min":  90, "max_energy_kwh": 0.42, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
        "30°C": {"duration_min":  90, "max_energy_kwh": 0.48, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
        "40°C": {"duration_min":  90, "max_energy_kwh": 0.52, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
        "60°C": {"duration_min":  90, "max_energy_kwh": 0.55, "heats":  True, "stable_min": 12, "max_dur_min": 110, "supports_anti_crease": True},
    }},  # Manual: 60°C -> cold; intermediate temps 20–40
    "outdoor":   {"label": "Outdoor",   "default_temp": "40°C", "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C"], "allowed_spin_speeds": SPIN_MAX_900, "default_temperature": "40°C", "default_spin": "900 rpm", "available_options": ["water_plus"], "by_temperature": {
        "cold": {"duration_min":  50, "max_energy_kwh": 0.25, "heats": False, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
        "20°C": {"duration_min":  50, "max_energy_kwh": 0.32, "heats":  True, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
        "30°C": {"duration_min":  50, "max_energy_kwh": 0.36, "heats":  True, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
        "40°C": {"duration_min":  50, "max_energy_kwh": 0.40, "heats":  True, "stable_min": 10, "max_dur_min":  65, "supports_anti_crease": True},
    }},  # Manual: 40°C -> cold; max spin 900 rpm
    "impraegnering": {"label": "Imprægnering", "default_temp": None, "allowed_temperatures": [], "default_temperature": None, "allowed_spin_speeds": SPIN_MAX_1200, "default_spin": "1200 rpm", "available_options": [], "duration_min":  25, "max_energy_kwh": 0.15, "heats": False, "stable_min":  8, "max_dur_min":  35, "supports_anti_crease": True},
    "pumpe_centrifugering": {"label": "Pumpe/Centrifugering", "default_temp": None, "allowed_temperatures": [], "default_temperature": None, "allowed_spin_speeds": CANONICAL_SPIN, "default_spin": "1400 rpm", "available_options": [], "duration_min":  10, "max_energy_kwh": 0.08, "heats": False, "stable_min":  5, "max_dur_min":  20, "supports_anti_crease": False},
    "kun_skyl_stivelse": {"label": "Kun skyl/stivelse", "default_temp": None, "allowed_temperatures": [], "default_temperature": None, "allowed_spin_speeds": CANONICAL_SPIN, "default_spin": "1400 rpm", "available_options": [], "duration_min":  30, "max_energy_kwh": 0.12, "heats": False, "stable_min":  8, "max_dur_min":  45, "supports_anti_crease": True},
    "unknown":   {"label": "Unknown",   "default_temp": None,   "allowed_temperatures": ["Cold", "20°C", "30°C", "40°C", "60°C", "90°C"], "allowed_spin_speeds": CANONICAL_SPIN, "default_temperature": None, "default_spin": "1400 rpm", "available_options": [], "duration_min": 180, "max_energy_kwh": 2.50, "heats":  None, "stable_min": 15, "max_dur_min": 240, "supports_anti_crease": True},
}


# Map from the human-readable input_select labels back to programme keys.
# Temperature is always a separate dimension read from temperature_entity.
#
# HA contract: confirm_entity (Washer Confirmed Programme) must have options
# programme name only: Auto (unconfirmed), Ekspres, Uld, Bomuld, Finvask,
# Strygelet, ECO. Temperature and spin are separate helpers (temperature_entity,
# spin_entity). Align HA helpers via MCP (ha_config_set_helper) or UI so the
# dropdowns match; the app can call input_select.set_options at startup to
# re-apply programme options if the helper was reverted.
LABEL_TO_KEY = {
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


# Legacy input_select options that carried a temperature suffix. Re-applied on top
# of any YAML-derived label map so old helper values still resolve.
LEGACY_LABEL_TO_KEY = [
    ("Ekspres 20", "ekspres"), ("Uld 30", "uld"), ("Bomuld 20", "bomuld"), ("Bomuld 60", "bomuld"),
    ("Finvask 40", "finvask"), ("Strygelet 30", "strygelet"), ("ECO 40-60", "eco"),
]

def get_profile(profiles: dict, programme: str, temperature=None):
    """Return the flat profile dict for a programme + optional temperature.

    For 'bomuld' with by_temperature, resolves to the matching sub-profile.
    Falls back to the first sub-profile if temperature is missing/unknown.
    For other programmes, returns the top-level profile directly.
    """
    prof = profiles.get(programme, profiles.get("unknown", {}))
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


def programme_has_temperature(profiles: dict, programme: str) -> bool:
    """Return True if this programme has temperature-dependent profiles (e.g. bomuld).
    Only then do we persist/learn temperature; otherwise learn_key is just programme."""
    prof = profiles.get(programme, profiles.get("unknown", {}))
    return isinstance(prof, dict) and "by_temperature" in prof


def learn_key_for(profiles: dict, programme: str, temperature) -> str:
    """Build the "prog|temp" learning key, collapsing to just "prog" when the
    programme has no temperature dimension. Same rule used everywhere a learned
    duration, centroid, or validation key is looked up."""
    if temperature and programme_has_temperature(profiles, programme):
        return f"{programme}|{temperature}"
    return programme


def merge_profiles(yaml_profiles: dict) -> dict:
    """Merge YAML programme profiles on top of DEFAULT_PROFILES.

    YAML overrides/extends only - we never lose e.g. 'unknown' or any default key.
    For programmes with by_temperature, a YAML file may override a subset of temps.
    Returns the merged dict; the caller assigns it to WasherMonitor.PROGRAMME_PROFILES.
    """
    merged = copy.deepcopy(DEFAULT_PROFILES)
    for key, val in yaml_profiles.items():
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
        merged["unknown"] = copy.deepcopy(DEFAULT_PROFILES["unknown"])
    return merged


def build_label_to_key(merged: dict, display_order) -> dict:
    """Build label -> key from merged profiles and the stable display order, so new
    YAML programmes appear. Legacy temperature-suffixed labels are appended last."""
    label_to_key = {}
    for key in display_order:
        if key in merged:
            label = merged[key].get("label", key)
            label_to_key[label] = key
    if "unknown" in merged:
        label_to_key[merged["unknown"].get("label", "Unknown")] = "unknown"
    # Legacy options with temperature suffix (backwards compatibility)
    for leg_label, leg_key in LEGACY_LABEL_TO_KEY:
        label_to_key[leg_label] = leg_key
    return label_to_key


def blend_learned_duration(manual: int, learned) -> int:
    """Blend the manual profile duration with the learned average.

    Confidence ramps with sample count n: 30% learned at n=1, 50% at n=2, then
    0.6 rising to a 0.9 cap from n=3 upward. Returns `manual` unchanged when
    there is no usable learned record.
    """
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
