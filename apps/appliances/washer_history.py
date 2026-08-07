"""
Normalization of AppDaemon/HA history payloads for the washer monitor.

Every history reader in washer_monitor.py (power, energy, state, door) has to do
the same three things before it can reason about anything: parse the ISO
timestamp, flatten AppDaemon's nested get_history shape, and turn raw entries
into (datetime, value) points. Those three live here, pure and free of `self`,
so washer_power.py can depend on them without reaching back into the app class.

identify_cycles lives here for the same reason: it reconstructs past cycles out of
four raw recorder series for the offline analysis report, rather than deciding
anything about the live machine.

Sibling-import module (AppDaemon puts app dirs on sys.path), matching the
existing `import climate_model as cm` precedent in apps/climate/.
"""

from datetime import datetime, timezone

# HA reports these instead of a number when a sensor has no usable value.
NON_NUMERIC_STATES = ("unknown", "unavailable", "")


def parse_utc(s: str):
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


def flatten_history(hist, entity_id=None):
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


def parse_power_points(hist):
    """Turn flattened history entries into [(datetime_utc, watts)], dropping anything
    without a usable timestamp or a numeric state. Order is left as-is - callers that
    need chronological order sort afterwards, exactly as they did inline."""
    points = []
    for entry in hist:
        try:
            ts_str = entry.get("last_changed") or entry.get("last_updated")
            if not ts_str:
                continue
            t = parse_utc(ts_str)
            if t is None:
                continue
            s = entry.get("state")
            if s is None or s in NON_NUMERIC_STATES:
                continue
            points.append((t, float(s)))
        except (ValueError, TypeError, AttributeError):
            continue
    return points


def identify_cycles(energy_hist, door_hist, power_hist, state_hist,
                    start_w, stop_w, high_power_threshold, low_power_threshold):
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
                timestamp = parse_utc(timestamp_str)
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
                if power >= start_w:
                    high_power_count += 1
                    if high_power_count >= high_power_threshold:
                        # Confirmed cycle start - use first high power reading
                        start_idx = max(0, idx - high_power_threshold + 1)
                        current_start = power_readings[start_idx][0]
                        high_power_count = 0
                        low_power_count = 0
                        low_power_start_time = None
                else:
                    high_power_count = 0
            
            # Cycle end detection: power drops and stays low
            elif current_start is not None:
                if power <= stop_w:
                    if low_power_start_time is None:
                        low_power_start_time = timestamp
                    low_power_count += 1
                    
                    # Check if we've had enough consecutive low readings
                    if low_power_count >= low_power_threshold:
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
                timestamp = parse_utc(timestamp_str)
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
                    timestamp = parse_utc(entry.get("last_changed", ""))
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
