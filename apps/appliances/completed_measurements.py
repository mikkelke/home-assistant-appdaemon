"""The published measurements of a finished appliance cycle are a snapshot.

Emptying and a dishwasher peek change the handling of a completed load, not its
duration or energy. Keep the existing attribute values, including missing values
after HA erased an entity; a surviving start clock is not a completion clock.
Programme corrections and false-finish recovery remain the monitors' decisions.
"""


def completed_measurements(state, full_state):
    """Return completed measurements, or None when this is still a live cycle.

    The distinction between None (not finished) and {} (finished, measurements
    unknown) is intentional: only the former permits measuring a first completion.
    Values are copied verbatim so each monitor keeps its public rounding, types,
    zero/None conventions, and duration-correction metadata. The HA entity is the
    snapshot source; this neither migrates old ledgers nor invents missing metrics.
    """
    if state not in ("Unemptied", "Emptied"):
        return None
    # AppDaemon's full-state cache can lag a successful completion publish. Its
    # Running/Paused energy is progress, not a completed measurement. Return an
    # empty update so the host merges into HA's newer summary without replaying
    # stale fields, and never interprets the missing snapshot as a fresh finish.
    if not isinstance(full_state, dict) or full_state.get("state") not in ("Unemptied", "Emptied"):
        return {}
    attributes = full_state.get("attributes") or {}
    return {
        key: attributes[key]
        for key in ("run_time_minutes", "energy_used", "end_reason", "idle_min")
        if key in attributes
    }
