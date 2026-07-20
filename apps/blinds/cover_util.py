"""
Shared cover-position interpretation for blind/cover-consuming apps.

Centralizes the single question "is this blind closed?" so every consumer
applies the same threshold semantics. A low-battery blind that parks at 99%
must read as *closed* everywhere - otherwise night-time auto-lighting fires
when the user expects darkness. Before this module, some apps used a proper
threshold (bedroom_blind_control / wakeup_bedroom) while others hardcoded an
exact ``>= 100`` check that silently failed at 99%.

Plain functions taking the AppDaemon Hass instance as the first arg, mirroring
``lighting_actions`` / ``room_state_darkness``. This module *reads* only -
physical re-commanding of covers stays local/opt-in in each app.

Cover convention:
  * ``closed_is_100=True``  (default): ``current_position`` 100 = fully closed,
    0 = fully open. "Closed" means ``position >= threshold``.
  * ``closed_is_100=False`` (inverted cover): 0 = fully closed, 100 = fully
    open. "Closed" means ``position <= (100 - threshold)`` - i.e. at
    ``threshold=95`` a position of 0-5 counts as closed (mirrors the
    ``pos == 0`` test in ``wakeup_bedroom._nudge_cover_if_closed``).
"""

from __future__ import annotations

from typing import Any

_UNKNOWN_STRINGS = ("", "unknown", "unavailable", "none")


def position(hass: Any, entity: str | None, default: int | None = None) -> int | None:
    """Current cover position (0-100) from the ``current_position`` attribute.

    Robust to a missing entity, a missing attribute, ``None`` /
    ``"unknown"`` / ``"unavailable"`` and non-numeric strings - returns
    ``default`` in every such case. Numeric strings (e.g. ``"99"`` /
    ``"99.0"``) are parsed; floats are truncated to ``int``.
    """
    if not entity:
        return default
    try:
        raw = hass.get_state(entity, attribute="current_position")
    except Exception:
        return default
    if raw is None:
        return default
    # bool is an int subclass but is never a valid position.
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if s.lower() in _UNKNOWN_STRINGS:
            return default
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return default
    return default


def is_closed(
    hass: Any,
    entity: str | None,
    threshold: int = 95,
    closed_is_100: bool = True,
) -> bool:
    """True when the cover is (approximately) fully closed.

    ``threshold`` is expressed as "percent closed": a normal cover
    (``closed_is_100``) is closed at ``current_position >= threshold``; an
    inverted cover is closed at ``current_position <= 100 - threshold``.
    Returns ``False`` when the position is unavailable (unknown != closed).
    """
    pos = position(hass, entity)
    if pos is None:
        return False
    if closed_is_100:
        return pos >= threshold
    return pos <= (100 - threshold)


def is_open(
    hass: Any,
    entity: str | None,
    threshold: int = 95,
    closed_is_100: bool = True,
) -> bool:
    """True when the cover is known to be not-closed.

    Not a plain ``not is_closed``: an unavailable position is neither closed
    nor open, so both predicates return ``False`` in that case.
    """
    pos = position(hass, entity)
    if pos is None:
        return False
    if closed_is_100:
        return pos < threshold
    return pos > (100 - threshold)
