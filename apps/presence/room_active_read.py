"""
room_active_read - plain-function reader for binary_sensor.<zone>_active (published by
room_active.RoomActive). No AppDaemon primitives beyond hass.get_state/hass.get_app - no
run_in/timers/service calls - so this is safe to call synchronously from both sync and async
AppDaemon apps without tripping the sync-from-async gotcha (an un-awaited async-side call to
a coroutine-returning API silently returns a dead coroutine that never runs).

Bias throughout: unknown/unreadable never collapses to "empty" - see active()'s resolution
order and explicitly_inactive()'s stricter bar below.
"""

from __future__ import annotations

from datetime import datetime, timezone


def active(hass, zone: str, max_age_sec: int = 300) -> bool | None:
    """True/False when determinable, None when no opinion. Callers must treat None as
    'make no change' (matches follow_me._is_present's existing tri-state).

    Resolution order:
      1. Read binary_sensor.<zone>_active and its computed_at attribute. If the state is
         "on"/"off" and computed_at parses as a valid ISO timestamp within max_age_sec of
         now (UTC), trust it directly.
      2. Otherwise fall back to recomputing live from RoomActive's own witness config
         (self.get_app("RoomActive").zones) - the exact same union-of-witnesses rule the
         publisher itself uses. If the app isn't loaded, or the zone is unknown to it, or
         every witness is unreadable, return None (unknown, never a false "empty").
    """
    entity_id = f"binary_sensor.{zone}_active"
    try:
        state = hass.get_state(entity_id)
    except Exception:
        state = None

    if state in ("on", "off"):
        try:
            computed_at = hass.get_state(entity_id, attribute="computed_at")
        except Exception:
            computed_at = None
        age = _age_seconds(computed_at)
        if age is not None and age <= max_age_sec:
            return state == "on"

    return _fallback(hass, zone)


def explicitly_inactive(hass, zone: str) -> bool:
    """True ONLY on a fresh, positive reading that the zone is empty - i.e. active() above
    returns exactly False. NEVER True from a missing/stale entity or any exception. Callers
    use this for 'is it SAFE to conclude nobody is here', a stricter bar than 'not active'."""
    try:
        return active(hass, zone) is False
    except Exception:
        return False


def _age_seconds(computed_at):
    """Seconds between now (UTC) and an ISO-8601 timestamp, or None if unparseable/absent."""
    if not computed_at:
        return None
    try:
        ts = datetime.fromisoformat(str(computed_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (TypeError, ValueError):
        return None


def _fallback(hass, zone):
    """Recompute live from RoomActive.zones - the publisher's own union-of-witnesses rule,
    reproduced exactly (each witness's on_states, default ["on"])."""
    try:
        room_active_app = hass.get_app("RoomActive")
    except Exception:
        return None
    if room_active_app is None:
        return None

    try:
        witnesses = room_active_app.zones.get(zone)
    except Exception:
        return None
    if not witnesses:
        return None

    any_readable = False
    for w in witnesses:
        try:
            entity = w["entity"]
            on_states = w.get("on_states") or ["on"]
            state = hass.get_state(entity)
        except Exception:
            continue
        if state in (None, "unavailable", "unknown"):
            continue
        any_readable = True
        if state in on_states:
            return True

    return False if any_readable else None
