"""
Lighting brightness for room apps - reads committed state from ``darkness_calculator``.

Per-zone thresholds live only in ``darkness_calculator.yaml``. This module maps
``sensor.room_state_*`` / ``sensor.darkness_*`` to auto-on / auto-off decisions.

Anti-flap contract (2026-06): decisions follow the **confirmed** classification.
``pending_target`` may only help keep light on:
  - ``pending_target == "dark"`` -> treated as dark (lamps may come on early, and a
    bright room about to flip dark is never auto-offed into darkness).
  - ``pending_target == "bright"`` is IGNORED - an unconfirmed bright must never
    turn lights off (this bypass was the main cause of light flapping).

See ``LIGHTING_STANDARD.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

RuleName = Literal[
    "missing_entity",
    "pending_dark",
    "pending_bright",
    "confirmed_dark",
    "confirmed_bright",
    "default",
]


@dataclass(frozen=True)
class RoomLightingContext:
    """Parsed inputs from a ``sensor.room_state_*`` entity."""

    entity_id: str | None
    state_text: str | None
    pending_target: str | None
    indoor_lux: float | None
    outdoor_lux: float | None
    bright_threshold: float | None
    dark_threshold: float | None


@dataclass(frozen=True)
class LightingDecision:
    """Result of brightness evaluation for logging and automation."""

    is_dark: bool
    rule: RuleName
    detail: str

    @property
    def is_bright(self) -> bool:
        return not self.is_dark


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_label_dark(state_text: str | None) -> bool | None:
    if not state_text or not isinstance(state_text, str):
        return None
    if state_text.lower() in ("on", "off"):
        return state_text.lower() == "on"
    m = re.search(r"\((dark|bright)\)", state_text, re.I)
    if m:
        return m.group(1).lower() == "dark"
    st = state_text.lower()
    if "dark" in st:
        return True
    if "bright" in st:
        return False
    return None


def read_room_lighting_context(
    hass: Any,
    entity_id: str | None,
) -> RoomLightingContext:
    """Load current room_state entity state and brightness-related attributes."""
    if not entity_id:
        return RoomLightingContext(
            entity_id=None,
            state_text=None,
            pending_target=None,
            indoor_lux=None,
            outdoor_lux=None,
            bright_threshold=None,
            dark_threshold=None,
        )

    pending = None
    state_text = None
    attrs: dict | None = None
    try:
        pending = hass.get_state(entity_id, attribute="pending_target")
    except Exception:
        pass
    try:
        state_text = hass.get_state(entity_id)
    except Exception:
        pass
    try:
        full = hass.get_state(entity_id, attribute="all")
        raw_attrs = (full or {}).get("attributes") if isinstance(full, dict) else None
        if isinstance(raw_attrs, dict):
            attrs = raw_attrs
    except Exception:
        pass

    return RoomLightingContext(
        entity_id=entity_id,
        state_text=state_text if isinstance(state_text, str) else None,
        pending_target=pending if isinstance(pending, str) else None,
        indoor_lux=_float_or_none((attrs or {}).get("indoor_lux")),
        outdoor_lux=_float_or_none((attrs or {}).get("outdoor_lux")),
        bright_threshold=_float_or_none((attrs or {}).get("bright_threshold")),
        dark_threshold=_float_or_none((attrs or {}).get("dark_threshold")),
    )


def is_confirmed_dark(
    hass: Any,
    room_state_entity: str | None,
    darkness_sensor: str | None = None,
    *,
    default_when_unknown: bool = False,
) -> bool:
    """
    Committed dark from ``darkness_calculator`` (per-zone yaml thresholds + confirm timers).

    Prefer ``sensor.darkness_<zone>`` when provided; else ``(Dark|Bright)`` in ``sensor.room_state_*``.
    """
    try:
        if darkness_sensor:
            s = hass.get_state(darkness_sensor)
            if s == "dark":
                return True
            if s == "bright":
                return False
        if room_state_entity:
            label = _parse_label_dark(hass.get_state(room_state_entity))
            if label is True:
                return True
            if label is False:
                return False
    except Exception:
        pass
    return default_when_unknown


def is_confirmed_bright(
    hass: Any,
    room_state_entity: str | None,
    darkness_sensor: str | None = None,
    *,
    default_when_unknown: bool = False,
) -> bool:
    """Committed bright - same sources as ``is_confirmed_dark``."""
    try:
        if darkness_sensor:
            s = hass.get_state(darkness_sensor)
            if s == "bright":
                return True
            if s == "dark":
                return False
        if room_state_entity:
            label = _parse_label_dark(hass.get_state(room_state_entity))
            if label is False:
                return True
            if label is True:
                return False
    except Exception:
        pass
    return default_when_unknown


def _confirmed_detail(
    ctx: RoomLightingContext,
    darkness_sensor: str | None,
    hass: Any,
) -> str:
    parts = []
    if darkness_sensor:
        parts.append(f"{darkness_sensor}={hass.get_state(darkness_sensor)}")
    if ctx.state_text:
        parts.append(f"state={ctx.state_text!r}")
    return ", ".join(parts) if parts else "confirmed"


def _evaluate_auto(
    hass: Any,
    entity_id: str | None,
    *,
    default_dark: bool,
    darkness_sensor: str | None,
    for_auto_off: bool,
) -> LightingDecision:
    ctx = read_room_lighting_context(hass, entity_id)
    if not ctx.entity_id:
        return LightingDecision(
            is_dark=default_dark,
            rule="missing_entity",
            detail=f"default_dark={default_dark}",
        )

    # pending_dark only helps: early ON, and blocks auto-off while trending dark.
    # pending_bright is ignored - only CONFIRMED bright may turn lights off.
    if ctx.pending_target == "dark":
        return LightingDecision(is_dark=True, rule="pending_dark", detail="pending_target=dark")

    detail = _confirmed_detail(ctx, darkness_sensor, hass)
    if for_auto_off:
        if is_confirmed_bright(
            hass, entity_id, darkness_sensor, default_when_unknown=False
        ):
            return LightingDecision(is_dark=False, rule="confirmed_bright", detail=detail)
        if is_confirmed_dark(
            hass, entity_id, darkness_sensor, default_when_unknown=False
        ):
            return LightingDecision(is_dark=True, rule="confirmed_dark", detail=detail)
    else:
        if is_confirmed_dark(
            hass, entity_id, darkness_sensor, default_when_unknown=default_dark
        ):
            return LightingDecision(is_dark=True, rule="confirmed_dark", detail=detail)
        if is_confirmed_bright(
            hass, entity_id, darkness_sensor, default_when_unknown=False
        ):
            return LightingDecision(is_dark=False, rule="confirmed_bright", detail=detail)

    return LightingDecision(
        is_dark=default_dark,
        rule="default",
        detail=f"default_dark={default_dark}",
    )


def evaluate_auto_on(
    hass: Any,
    entity_id: str | None,
    *,
    default_dark: bool = True,
    darkness_sensor: str | None = None,
) -> LightingDecision:
    """Dark enough to auto-on: pending dark, else committed dark from calculator."""
    return _evaluate_auto(
        hass,
        entity_id,
        default_dark=default_dark,
        darkness_sensor=darkness_sensor,
        for_auto_off=False,
    )


def evaluate_auto_off(
    hass: Any,
    entity_id: str | None,
    *,
    default_dark: bool = True,
    darkness_sensor: str | None = None,
) -> LightingDecision:
    """Bright enough to auto-off while occupied: pending bright, else committed bright."""
    return _evaluate_auto(
        hass,
        entity_id,
        default_dark=default_dark,
        darkness_sensor=darkness_sensor,
        for_auto_off=True,
    )


def is_dark_for_auto_on(
    hass: Any,
    entity_id: str | None,
    default_dark: bool = True,
    **kwargs: Any,
) -> bool:
    return evaluate_auto_on(hass, entity_id, default_dark=default_dark, **kwargs).is_dark


def is_dark_for_auto_off(
    hass: Any,
    entity_id: str | None,
    default_dark: bool = True,
    **kwargs: Any,
) -> bool:
    return evaluate_auto_off(hass, entity_id, default_dark=default_dark, **kwargs).is_dark


def is_dark_for_lights(hass: Any, entity_id: str | None, default_dark: bool = True, **kwargs: Any) -> bool:
    """Backward-compatible alias - same semantics as ``is_dark_for_auto_on``."""
    return is_dark_for_auto_on(hass, entity_id, default_dark=default_dark, **kwargs)


def format_decision_log(decision: LightingDecision, *, prefix: str = "") -> str:
    """Human-readable line for AppDaemon logs."""
    bright_dark = "dark" if decision.is_dark else "bright"
    p = f"{prefix}: " if prefix else ""
    return f"{p}{bright_dark} [{decision.rule}] {decision.detail}"
