"""
Shared brightness-ramp actions - the "how" behind a slow fade-in.

``wakeup_bedroom`` used to be the only app besides ``guest_bathroom_lights`` that
called ``light/turn_on`` itself. That meant the sunrise ramp carried its own copy of
"what it means to light this room": the service name, the transition, the Adaptive
Lighting pause, the arithmetic of the next step. A change to how the bedroom is lit
had to be made twice, and the two copies were free to drift.

This module owns those mechanics. The wake routine keeps deciding *when* to ramp and
*when to stop* (bed session, darkness, the wake window) - that is orchestration, and
it is legitimately its job. Everything about *how* the light actually moves lives
here.

Plain functions taking the AppDaemon Hass instance first, mirroring
``lighting_actions`` / ``room_state_darkness`` / ``cover_util``. No ``appdaemon``
import and no module-level state, so a caller can import it defensively and degrade
to a direct service call if it is ever missing (see ``LIGHTING_STANDARD.md``).

Adaptive Lighting: the ramp drives brightness by hand, so AL's brightness adaptation
has to be paused for the duration or the two fight each other every tick. Pausing and
resuming is part of the ramp's mechanics, not the caller's, so both live here.
"""

from __future__ import annotations

from typing import Any, Callable

#: Home Assistant never accepts 0 % as "on" - a ramp always starts at 1 or above.
MIN_PCT = 1
MAX_PCT = 100


def clamp_pct(pct: Any, low: int = MIN_PCT, high: int = MAX_PCT) -> int:
    """Coerce anything numeric-ish to a legal brightness percentage."""
    try:
        value = int(pct)
    except (TypeError, ValueError):
        value = low
    return max(low, min(high, value))


def start_pct(configured_start: Any) -> int:
    """First step of a ramp: never below 1 %, however the YAML is written."""
    return clamp_pct(configured_start)


def next_pct(current: Any, step: Any, target: Any) -> int:
    """The step after ``current``, never overshooting ``target``."""
    try:
        cur = int(current)
    except (TypeError, ValueError):
        cur = MIN_PCT
    try:
        inc = int(step)
    except (TypeError, ValueError):
        inc = 1
    try:
        cap = int(target)
    except (TypeError, ValueError):
        return cur
    return min(cur + inc, cap)


def resolve_target(adaptive_target: Any, ramp_max: Any) -> int:
    """Where the ramp is heading: Adaptive Lighting's current idea of the right
    brightness, capped by the ramp's own ceiling. A missing AL reading falls back to
    the ceiling, which is what an unattended ramp used to do anyway."""
    try:
        cap = int(ramp_max)
    except (TypeError, ValueError):
        cap = MAX_PCT
    if adaptive_target is None:
        return cap
    try:
        return min(int(adaptive_target), cap)
    except (TypeError, ValueError):
        return cap


def set_brightness(
    hass: Any,
    light_entity: str,
    brightness_pct: Any,
    *,
    transition: Any = None,
) -> int:
    """The single place the ramp touches a light.

    Emits exactly the ``light/turn_on`` call the wake routine used to make inline,
    so routing through here changes no behaviour - only where the knowledge lives.
    """
    pct = clamp_pct(brightness_pct)
    kwargs: dict[str, Any] = {"entity_id": light_entity, "brightness_pct": pct}
    if transition is not None:
        try:
            kwargs["transition"] = int(transition)
        except (TypeError, ValueError):
            pass
    hass.call_service("light/turn_on", **kwargs)
    return pct


def pause_adaptive_brightness(hass: Any, switch: str | None) -> None:
    """Hand brightness to the ramp. Never raises: a ramp that cannot pause AL is
    still far better than no wake-up light at all."""
    if not switch:
        return
    try:
        hass.turn_off(switch)
    except Exception:
        pass


def resume_adaptive_brightness(
    hass: Any,
    switch: str | None,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    """Give brightness back to Adaptive Lighting at the end of a ramp.

    Also never raises - this runs from teardown paths where an exception would
    strand the switch off with nothing left to restore it (the exact state an AD
    restart mid-ramp leaves behind; see ``wakeup_bedroom``'s init reconcile)."""
    if not switch:
        return
    try:
        if log_fn:
            log_fn("Restoring Adaptive Lighting brightness adaptation")
        hass.turn_on(switch)
    except Exception:
        pass
