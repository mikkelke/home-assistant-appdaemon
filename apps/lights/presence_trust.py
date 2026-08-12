"""
Presence trust - recompute the distrust rules for interference-prone presence.

Shared helper in the lights-apps convention (plain functions, Hass instance
first, no yaml registration - like ``room_state_darkness`` / ``lighting_actions``).

Why: the FP300 mmWave sensors latch on non-human motion. Catalogue (kept by
``presence_model.py``, which publishes the same signal as a *shadow* flag on
``binary_sensor.presence_<room>``): kitchen speaker cone micro-motion, AC
airflow in the bedroom, AC condenser hum in the bathroom. Measured
2026-08-05..08-11: 575 min of mmWave-only kitchen presence with the PIR never
firing once - impossible for a real person over 160 min - 88 min of it with
island + counter lights on.

This module recomputes the rule from the RAW member sensors. It deliberately
does NOT read ``binary_sensor.presence_*``: those are ``set_state`` entities
that vanish on HA restart, so they are unacceptable as a load-bearing input
(and their ``suspect`` attribute silently drops from attributes whenever it is
False - AppDaemon 4.5.13 ``set_state`` bug - absence means False).

SUSPECT definition (presence_model's published logic plus a duration guard):
  - the room's composite (``binary_sensor.<room>_pir_presence``) is ``on``, AND
  - the room's configured interferer is active (kitchen:
    ``media_player.kitchen_2`` playing; bedroom/bathroom:
    ``sensor.air_conditioner_real_time_power`` > 50 W), AND
  - the marker-matching (mmWave) member(s) have held presence continuously for
    at least ``suspect_after_minutes`` (their ``last_changed`` is the on-edge), AND
  - no non-marker (PIR) member fired anywhere in that span. "Fired" = the PIR
    is on right now, or its ``last_changed`` (the off-edge of its most recent
    activation) lies inside the span. A person who walked in fires the PIR at
    entry, so a real entrance can never become suspect for the whole
    continuous mmWave-on span.

Consumption semantics (asymmetric - this is the core contract):
  - SUSPECT presence must NEVER trigger an auto-ON (lights on, speaker
    unmute, dishwasher signal).
  - SUSPECT presence MUST still count as presence for the auto-OFF hold - a
    real person standing still is never plunged into darkness or silence.

Trust bias: every unreadable input (missing entity, membership not a list
where a group is expected, unparseable ``last_changed``) resolves to NOT
suspect - a failure inside this helper must never strand a real person with
blocked auto-on. The one deliberate exception mirrors presence_model: a
composite whose membership is by-design not introspectable (template
composite, e.g. bathroom) falls back to the composite's own on-edge as the
span and is suspect-eligible without PIR evidence.

Bedroom/bathroom rules are implemented but NOT consumed anywhere yet - the AC
is un-deployed, so they cannot be validated against live interference.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_SUSPECT_AFTER_MINUTES = 10.0

# Mirror of apps/lights/presence_model.yaml ``distrust_rules`` - that yaml stays
# the source of truth for WHICH interferers exist; keep this dict in sync by
# hand when a new ghost source is catalogued there. A rule may additionally
# carry ``suspect_after_minutes`` to tune the duration guard per room.
DEFAULT_DISTRUST_RULES: dict[str, dict[str, Any]] = {
    "kitchen": {
        "when_entity": "media_player.kitchen_2",
        "when_state": "playing",
        "marker": "presence_presence",
        "label": "kitchen speaker playing (cone micro-motion)",
    },
    "bedroom": {
        # NOT WIRED to any consumer: AC un-deployed, rule unvalidated. Note the
        # marker also matches binary_sensor.bedroom_presence_pir_detection (the
        # FP300's own PIR) exactly as in presence_model.yaml - only the bedside
        # sensors count as trusted members. Revisit before wiring.
        "when_entity": "sensor.air_conditioner_real_time_power",
        "when_above": 50,
        "marker": "bedroom_presence",
        "label": "AC running (airflow/curtain ghost)",
    },
    "bathroom": {
        # NOT WIRED - same reason as bedroom. The bathroom composite is a
        # template (members not introspectable), so this rule uses the
        # composite-on-edge fallback span.
        "when_entity": "sensor.air_conditioner_real_time_power",
        "when_above": 50,
        "marker": "bathroom_presence",
        "label": "AC condenser in bathroom",
    },
}


@dataclass(frozen=True)
class PresenceTrustResult:
    """Outcome of the distrust evaluation for one room."""

    suspect: bool
    reason: str


def _rule_active(hass: Any, rule: dict) -> bool:
    """Interferer check - mirrors ``presence_model._rule_active`` exactly."""
    if not rule or not rule.get("when_entity"):
        return False
    state = hass.get_state(rule["when_entity"])
    if "when_above" in rule:
        try:
            return float(state) > float(rule["when_above"])
        except (TypeError, ValueError):
            return False
    return state == rule.get("when_state", "on")


def _last_changed_ts(hass: Any, entity: str) -> float | None:
    """Epoch seconds of an entity's last state change, or None when unreadable."""
    try:
        raw = hass.get_state(entity, attribute="last_changed")
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def presence_suspect(
    hass: Any,
    room: str,
    composite_entity: str,
    *,
    suspect_after_minutes: float | None = None,
    rule: dict | None = None,
    now: float | None = None,
) -> PresenceTrustResult:
    """
    Evaluate whether ``composite_entity``'s current presence is SUSPECT.

    ``rule`` defaults to ``DEFAULT_DISTRUST_RULES[room]`` (rooms without a rule
    are never suspect). ``suspect_after_minutes`` overrides the rule's / module
    default duration guard. ``now`` (epoch seconds) is injectable for tests.

    Never raises; any internal error resolves to not-suspect (trust bias).
    """
    try:
        return _presence_suspect_inner(
            hass,
            room,
            composite_entity,
            suspect_after_minutes=suspect_after_minutes,
            rule=rule,
            now=now,
        )
    except Exception as e:  # noqa: BLE001 - trust bias, see module docstring
        return PresenceTrustResult(False, f"presence_trust error ({e}) - trusting presence")


def _presence_suspect_inner(
    hass: Any,
    room: str,
    composite_entity: str,
    *,
    suspect_after_minutes: float | None,
    rule: dict | None,
    now: float | None,
) -> PresenceTrustResult:
    if rule is None:
        rule = DEFAULT_DISTRUST_RULES.get(room)
    if not rule:
        return PresenceTrustResult(False, f"no distrust rule for {room}")

    state = hass.get_state(composite_entity)
    if state != "on":
        return PresenceTrustResult(False, f"composite {composite_entity} is {state}")

    if not _rule_active(hass, rule):
        return PresenceTrustResult(
            False, f"interferer inactive ({rule.get('when_entity')})"
        )

    minutes = suspect_after_minutes
    if minutes is None:
        minutes = rule.get("suspect_after_minutes", DEFAULT_SUSPECT_AFTER_MINUTES)
    try:
        after_s = float(minutes) * 60.0
    except (TypeError, ValueError):
        after_s = DEFAULT_SUSPECT_AFTER_MINUTES * 60.0

    if now is None:
        now = _time.time()

    label = rule.get("label", "interference source active")
    marker = rule.get("marker", "presence_")
    members = hass.get_state(composite_entity, attribute="entity_id")

    if isinstance(members, (list, tuple)) and members:
        marker_members = [m for m in members if marker in m]
        trusted_members = [m for m in members if marker not in m]

        on_markers = [m for m in marker_members if hass.get_state(m) == "on"]
        if not on_markers:
            return PresenceTrustResult(
                False, "composite held by a non-marker member - real presence"
            )

        edges = []
        for m in on_markers:
            ts = _last_changed_ts(hass, m)
            if ts is None:
                return PresenceTrustResult(False, f"{m} on-edge unknown - trusting")
            edges.append(ts)
        # Each on member has been continuously on since its own last_changed;
        # the earliest edge is when marker-only presence started being held.
        span_start = min(edges)
        span_s = now - span_start

        if span_s < after_s:
            return PresenceTrustResult(
                False, f"marker-only for {span_s:.0f}s < {after_s:.0f}s"
            )

        for m in trusted_members:
            if hass.get_state(m) == "on":
                return PresenceTrustResult(False, f"{m} is on - real presence")
            ts = _last_changed_ts(hass, m)
            if ts is None:
                # Cannot prove the PIR stayed silent - trust presence rather
                # than block auto-on on a dead/unreadable sensor.
                return PresenceTrustResult(False, f"{m} last_changed unknown - trusting")
            if ts >= span_start:
                return PresenceTrustResult(
                    False, f"{m} fired within the marker-on span"
                )

        return PresenceTrustResult(
            True,
            f"{label}: marker-only presence {span_s / 60.0:.0f} min, "
            f"PIR silent since before the span",
        )

    # Membership not introspectable (template composite). Mirror presence_model,
    # which treats these as suspect-eligible; the composite's own on-edge is the
    # best available span start.
    ts = _last_changed_ts(hass, composite_entity)
    if ts is None:
        return PresenceTrustResult(False, "composite on-edge unknown - trusting")
    span_s = now - ts
    if span_s < after_s:
        return PresenceTrustResult(
            False, f"composite on for {span_s:.0f}s < {after_s:.0f}s"
        )
    return PresenceTrustResult(
        True,
        f"{label}: composite on {span_s / 60.0:.0f} min (members not introspectable)",
    )
