"""
Shared occupancy + brightness actions for room light apps.

See ``LIGHTING_STANDARD.md``.
"""

from __future__ import annotations

from typing import Any, Callable

import room_state_darkness


def read_occupied_from_room_state(hass: Any, room_state_entity: str | None) -> bool | None:
    """Occupied flag pushed by ``darkness_calculator`` on ``sensor.room_state_*``."""
    if not room_state_entity:
        return None
    try:
        occ = hass.get_state(room_state_entity, attribute="occupied")
        if occ is not None:
            return str(occ).lower() in ("true", "on", "1", "yes")
        label = hass.get_state(room_state_entity)
        if isinstance(label, str) and label:
            low = label.lower()
            if "empty" in low:
                return False
            if "occupied" in low:
                return True
    except Exception:
        pass
    return None


def register_room_state_push_listeners(
    hass: Any,
    callback: Callable[..., None],
    *,
    room_state_entity: str | None,
    darkness_sensor: str | None = None,
) -> None:
    """React only when Brain A pushes (state, occupied, pending, darkness)."""
    if room_state_entity:
        hass.listen_state(callback, room_state_entity)
        hass.listen_state(callback, room_state_entity, attribute="occupied")
        hass.listen_state(callback, room_state_entity, attribute="pending_target")
    if darkness_sensor:
        hass.listen_state(callback, darkness_sensor)


def apply_global_lighting(
    hass: Any,
    *,
    room_state_entity: str | None,
    darkness_sensor: str | None,
    default_dark: bool,
    lights_on: bool,
    turn_on: Callable[[], None],
    turn_off: Callable[[], None],
    occupied: bool | None = None,
    block_auto_on: bool = False,
    log_fn: Callable[[str], None] | None = None,
    bright_lights_on: bool | None = None,
    turn_off_bright: Callable[[], None] | None = None,
) -> str:
    """
    Global standard: dark/bright from ``room_state`` push; optional explicit occupied
    (bedroom effective occupancy). Returns action from ``apply_occupied_brightness``.
    """
    if occupied is None:
        occupied = read_occupied_from_room_state(hass, room_state_entity)
    if occupied is None:
        occupied = not default_dark

    on_d = room_state_darkness.evaluate_auto_on(
        hass,
        room_state_entity,
        default_dark=default_dark,
        darkness_sensor=darkness_sensor,
    )
    off_d = room_state_darkness.evaluate_auto_off(
        hass,
        room_state_entity,
        default_dark=default_dark,
        darkness_sensor=darkness_sensor,
    )
    return apply_occupied_brightness(
        occupied=occupied,
        is_dark_for_on=on_d.is_dark,
        is_dark_for_off=off_d.is_dark,
        lights_on=lights_on,
        turn_on=turn_on,
        turn_off=turn_off,
        block_auto_on=block_auto_on,
        log_fn=log_fn,
        bright_lights_on=bright_lights_on,
        turn_off_bright=turn_off_bright,
    )


def apply_occupied_brightness(
    *,
    occupied: bool,
    is_dark_for_on: bool,
    is_dark_for_off: bool,
    lights_on: bool,
    turn_on: Callable[[], None],
    turn_off: Callable[[], None],
    block_auto_on: bool = False,
    log_fn: Callable[[str], None] | None = None,
    bright_lights_on: bool | None = None,
    turn_off_bright: Callable[[], None] | None = None,
) -> str:
    """
  Apply standard occupied + brightness rules.

  Returns action taken: ``off_vacant``, ``off_bright``, ``on_dark``, ``noop_dark_on``,
  ``noop_bright``, ``blocked``, ``noop``.

  ``bright_lights_on`` / ``turn_off_bright``: rooms with a sub-zone light that stays
  on in bright conditions (bathroom bath spot) pass the main-lights-only state and a
  mains-only off action here, so the occupied+bright shutoff cannot kill the sub-zone
  light. Vacancy shutoff always uses ``lights_on`` + ``turn_off`` (everything off).
    """
    if bright_lights_on is None:
        bright_lights_on = lights_on
    if turn_off_bright is None:
        turn_off_bright = turn_off

    if not occupied:
        if lights_on:
            turn_off()
            if log_fn:
                log_fn("Turning OFF (no occupancy)")
        return "off_vacant"

    if not is_dark_for_off:
        if bright_lights_on:
            turn_off_bright()
            if log_fn:
                log_fn("Turning OFF (bright + occupied - sun is sufficient)")
        return "off_bright" if bright_lights_on else "noop_bright"

    if block_auto_on:
        return "blocked"

    if not lights_on and is_dark_for_on:
        turn_on()
        if log_fn:
            log_fn("Turning ON (dark + occupied)")
        return "on_dark"

    if lights_on:
        return "noop_dark_on"
    return "noop"


def log_lighting_decision(
    log_fn: Callable[[str], None],
    room_name: str,
    trigger: str,
    decision: room_state_darkness.LightingDecision,
    *,
    suffix: str = "",
) -> None:
    """Structured decision log for triage."""
    mode = "auto-on" if "on" in suffix else "auto-off" if "off" in suffix else ""
    extra = f" ({suffix})" if suffix else ""
    log_fn(
        f"{room_name}: [{trigger}] {mode} "
        f"{room_state_darkness.format_decision_log(decision).strip()}{extra}"
    )


def manual_override_active(hass: Any, override_entity: str | None) -> bool:
    """GLOBAL mechanism — per-room manual override. True while the room's
    ``*_lights_manual`` input_boolean is ON: the room app must not act on lights
    at all (no auto-on, no auto-off, no enforcement). Wall switches / manual
    control keep working. Apps re-evaluate when the toggle clears."""
    if not override_entity:
        return False
    try:
        return hass.get_state(override_entity) == "on"
    except Exception:
        return False
