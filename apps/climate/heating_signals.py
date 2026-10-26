"""
Signals for the floor-heating controller: who changed a thermostat, a nudge button or a helper,
and what that means for the plan. Pure functions and small state objects, no AppDaemon import and
no I/O, so every rule can be unit-tested on its own.

Nothing here writes anything. The controller records would_* effects for shadow zones; live
zones restore foreign setpoint and mode changes while the owner's off choice is held. Nudges
from buttons and shadow-zone resident changes move the zone's persisted nudge.

Attribution of a thermostat change. A state change does not reliably carry its caller (the
device echoes seconds later), so the change is matched to the climate service calls seen on the
same entity shortly before it. A call that explains the change decides who made it:
  - from a person's account: that person, through Home Assistant
  - from AppDaemon's account: another app
  - with a parent context and no user: a Home Assistant automation
  - from an account nobody maps: unknown
No explaining call means the keypad, credited to the room's resident when the zone has exactly
one. The controller's own writes are excluded first; a return to the pre-write value or mode
without an explaining call is attributed to a device rejection.

Meaning, by who and what:
  - A resident's button press, or their setpoint change in their own room (Home Assistant or
    keypad), moves their nudge one step. The owner's button press does the same.
  - In shadow, the owner moving a thermostat records one step on its target helper. Live
    thermostat setpoint changes restore the plan, including the owner's direct changes.
  - The owner changing a resident room's target helper resets that room's nudge.
  - The owner switching a zone off is the owner's choice; anyone else is restored.
  - Anything else (another zone's resident, an automation, another app, an unknown keypad) leaves
    the target alone and would be restored.
  - A helper the owner owns, changed by anyone but the owner, AppDaemon or Home Assistant itself
    (a change with no context at all), would be reverted.

Repeated signals in the same direction within NUDGE_COLLAPSE_S are one step; a signal the other
way is a new step. A nudge stays inside the zone's bounds and persists.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

import heating_plan as hp

CALL_EVENT = "call_service"
CALL_DOMAINS = ("climate",)
CALL_KEEP_S = 600.0
CALL_MATCH_S = 120.0
CALL_LATE_S = 2.0
OWN_WRITE_S = 600.0
DEVICE_REVERT_S = 180.0
NUDGE_COLLAPSE_S = 300.0
SETPOINT_MATCH_C = hp.SETPOINT_STEP_C + 0.01

KIND_SETPOINT = "setpoint"
KIND_MODE = "mode"
KIND_PRESET = "preset"
KIND_TARGET = "target"
KIND_BUTTON = "button"
KIND_HELPER = "helper"

SRC_BUTTON = "button"
SRC_HA = "ha"
SRC_KEYPAD = "keypad"
SRC_APP = "app"
SRC_AUTOMATION = "automation"
SRC_UNKNOWN = "unknown"
SRC_OWN = "own_write"
SRC_REVERT = "device_revert"

EFFECT_NONE = "none"
EFFECT_NUDGE = "nudge"
EFFECT_NUDGE_LIMIT = "nudge_limit"
EFFECT_NUDGE_COLLAPSED = "nudge_collapsed"
EFFECT_NUDGE_RESET = "nudge_reset"
EFFECT_TARGET_SET = "target_set"
EFFECT_WOULD_SET_TARGET = "would_set_target"
EFFECT_OFF_BY_OWNER = "off_by_owner"
EFFECT_WOULD_RESTORE = "would_restore"
EFFECT_RESTORE = "restore"
EFFECT_WOULD_REVERT = "would_revert"
EFFECT_IGNORED = "ignored"

DIRECTION_WORDS = {1: "warmer", -1: "colder"}


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or value in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_user_id(user_id) -> str:
    """A Home Assistant account id in context.user_id's own form: undashed lowercase hex."""
    return str(user_id or "").replace("-", "").strip().lower()


# ---------------------------------------------------------------- who


@dataclass(frozen=True)
class Actor:
    """kind is person, app, automation, unknown_user or none (no context at all)."""

    kind: str
    name: Optional[str] = None


@dataclass(frozen=True)
class Identity:
    owner: str
    app_user_id: str = ""
    users: dict = field(default_factory=dict)  # normalized account id -> person name

    def actor(self, user_id, parent_id) -> Actor:
        uid = normalize_user_id(user_id)
        if uid:
            if uid == normalize_user_id(self.app_user_id):
                return Actor("app", "appdaemon")
            name = self.users.get(uid)
            return Actor("person", name) if name else Actor("unknown_user")
        if parent_id:
            return Actor("automation")
        return Actor("none")

    def with_users(self, users: dict) -> "Identity":
        return replace(self, users=users)


def describe(actor: Actor) -> tuple:
    """(source, who) of an actor as the signal log names them."""
    if actor.kind == "person":
        return SRC_HA, actor.name
    if actor.kind == "app":
        return SRC_APP, actor.name
    if actor.kind == "automation":
        return SRC_AUTOMATION, None
    return SRC_UNKNOWN, None


def who_label(source: str, who: Optional[str]) -> str:
    if who:
        return who
    return "automation" if source == SRC_AUTOMATION else "unknown"


# ---------------------------------------------------------------- service calls


@dataclass(frozen=True)
class CallRecord:
    ts: float
    entity: str
    service: str
    data: dict
    user_id: Optional[str]
    parent_id: Optional[str]


def _entities(value) -> list:
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def call_records(event: dict, ts: float, watched) -> list:
    """The records of one service-call event: one per targeted entity that is in `watched`.
    Area and device targets are not resolved."""
    event = event if isinstance(event, dict) else {}
    service_data = event.get("service_data")
    service_data = service_data if isinstance(service_data, dict) else {}
    targets = [e for e in _entities(service_data.get("entity_id")) if e in watched]
    if not targets:
        return []
    metadata = event.get("metadata")
    context = metadata.get("context") if isinstance(metadata, dict) else None
    context = context if isinstance(context, dict) else {}
    return [
        CallRecord(
            ts=ts,
            entity=entity,
            service=str(event.get("service") or ""),
            data=dict(service_data),
            user_id=context.get("user_id") or None,
            parent_id=context.get("parent_id") or None,
        )
        for entity in targets
    ]


class CallBuffer:
    """The service calls of the last CALL_KEEP_S seconds."""

    def __init__(self):
        self._calls: list = []

    def add(self, record: CallRecord) -> None:
        self._calls.append(record)
        cutoff = record.ts - CALL_KEEP_S
        if self._calls[0].ts < cutoff:
            self._calls = [c for c in self._calls if c.ts >= cutoff]

    def recent(self, entity: str, ts: float, window_s: float = CALL_MATCH_S) -> list:
        """Calls on `entity` up to window_s before `ts` (and a moment after it, for events that
        arrive out of order), newest first."""
        found = [
            c for c in self._calls if c.entity == entity and -CALL_LATE_S <= ts - c.ts <= window_s
        ]
        return sorted(found, key=lambda c: c.ts, reverse=True)

    def __len__(self) -> int:
        return len(self._calls)


# ---------------------------------------------------------------- thermostat changes


@dataclass(frozen=True)
class Change:
    kind: str
    old: object
    new: object
    ts: float

    @property
    def direction(self) -> int:
        """sign(new - old): the magnitude of a change is ignored."""
        if self.kind not in (KIND_SETPOINT, KIND_TARGET):
            return 0
        old, new = _num(self.old), _num(self.new)
        if old is None or new is None:
            return 0
        return (new > old) - (new < old)


@dataclass(frozen=True)
class OwnWrite:
    """The controller's latest setpoint or mode write and the previous reported setpoint."""

    value: Optional[float]
    ts: float
    prev_reported: Optional[float] = None
    mode: Optional[str] = None
    prev_mode: Optional[str] = None


@dataclass(frozen=True)
class Attribution:
    source: str
    who: Optional[str] = None
    call: Optional[CallRecord] = None


def _is_own_call(call: CallRecord, own: OwnWrite) -> bool:
    """Whether a recorded call is the controller's own write, so it cannot explain a change."""
    data = call.data
    if abs(call.ts - own.ts) > CALL_MATCH_S:
        return False
    if own.value is None:
        return call.service == "set_hvac_mode" and data.get("hvac_mode") == own.mode
    asked = _num(data.get("temperature"))
    return (
        call.service == "set_temperature"
        and asked is not None
        and abs(asked - own.value) < 1e-6
        and data.get("hvac_mode") == own.mode
    )


def call_explains(call: CallRecord, change: Change) -> bool:
    """Whether a service call can have produced a change. A setpoint is matched within one
    device step, because the thermostat rounds what it is given."""
    data = call.data
    if change.kind == KIND_SETPOINT:
        asked, got = _num(data.get("temperature")), _num(change.new)
        return (
            call.service == "set_temperature"
            and asked is not None
            and got is not None
            and abs(asked - got) <= SETPOINT_MATCH_C
        )
    if change.kind == KIND_MODE:
        if call.service in ("set_hvac_mode", "set_temperature"):
            return data.get("hvac_mode") == change.new
        if call.service == "turn_off":
            return change.new == "off"
        if call.service == "turn_on":
            return change.new != "off"
        return call.service == "toggle"
    if change.kind == KIND_PRESET:
        return call.service == "set_preset_mode" and data.get("preset_mode") == change.new
    return False


def attribute_change(
    zc: hp.ZoneConfig,
    change: Change,
    calls: CallBuffer,
    identity: Identity,
    own: Optional[OwnWrite] = None,
) -> Attribution:
    """Who made a thermostat change, see the module docstring."""
    if own is not None:
        age = change.ts - own.ts
        if 0.0 <= age <= OWN_WRITE_S:
            if change.kind == KIND_MODE and own.mode is not None and change.new == own.mode:
                return Attribution(SRC_OWN)
            if change.kind == KIND_PRESET and own.mode is not None:
                return Attribution(SRC_OWN)
            if change.kind == KIND_SETPOINT:
                new = _num(change.new)
                if own.value is None and own.mode is not None:
                    return Attribution(SRC_OWN)
                if new is not None and own.value is not None and abs(new - own.value) < 1e-6:
                    return Attribution(SRC_OWN)
    for call in calls.recent(zc.thermostat, change.ts):
        if own is not None and _is_own_call(call, own):
            continue
        if call_explains(call, change):
            source, who = describe(identity.actor(call.user_id, call.parent_id))
            return Attribution(source, who, call)
    if own is not None and change.kind == KIND_SETPOINT:
        new = _num(change.new)
        if (
            new is not None
            and own.value is not None
            and own.prev_reported is not None
            and 0.0 <= change.ts - own.ts <= DEVICE_REVERT_S
            and abs(new - own.prev_reported) < 1e-6
        ):
            return Attribution(SRC_REVERT)
    if (
        own is not None
        and change.kind == KIND_MODE
        and own.mode is not None
        and own.prev_mode is not None
        and change.new == own.prev_mode != own.mode
        and 0.0 <= change.ts - own.ts <= DEVICE_REVERT_S
    ):
        return Attribution(SRC_REVERT)
    who = zc.residents[0] if len(zc.residents) == 1 else None
    return Attribution(SRC_KEYPAD, who)


def thermostat_effect(zc: hp.ZoneConfig, owner: str, change: Change, attr: Attribution) -> str:
    """What a thermostat change means. EFFECT_NUDGE is the intent to move the nudge; whether it
    moves, stops at a bound or collapses into an earlier step is step_nudge's call."""
    if attr.source in (SRC_OWN, SRC_REVERT):
        return EFFECT_NONE
    by_owner = attr.source == SRC_HA and attr.who == owner
    if change.kind == KIND_MODE:
        if change.new != "off":
            return EFFECT_NONE
        return EFFECT_OFF_BY_OWNER if by_owner else EFFECT_WOULD_RESTORE
    if change.kind == KIND_PRESET:
        return EFFECT_NONE if by_owner else EFFECT_WOULD_RESTORE
    if by_owner:
        return EFFECT_WOULD_SET_TARGET
    if attr.who in zc.residents and attr.source in (SRC_HA, SRC_KEYPAD):
        return EFFECT_NUDGE
    return EFFECT_WOULD_RESTORE


# ---------------------------------------------------------------- buttons and helpers


def button_allowed(zc: hp.ZoneConfig, owner: str, actor: Actor) -> bool:
    """A nudge button answers to the room's residents and to the owner."""
    return actor.kind == "person" and (actor.name == owner or actor.name in zc.residents)


def helper_effect(
    owner: str, actor: Actor, zc: Optional[hp.ZoneConfig] = None, nudge: float = 0.0
) -> str:
    """What a change of a helper the owner owns means. `zc` is the zone whose target helper it
    is, None for any other owned helper. A change that carries no context at all is Home
    Assistant itself (a restore, a reload), never a person, so it is never reverted."""
    if actor.kind == "person" and actor.name == owner:
        if zc is None:
            return EFFECT_NONE
        return EFFECT_NUDGE_RESET if zc.residents and nudge != 0 else EFFECT_TARGET_SET
    if actor.kind in ("app", "none"):
        return EFFECT_NONE if zc is None else EFFECT_TARGET_SET
    return EFFECT_WOULD_REVERT


# ---------------------------------------------------------------- nudge


def step_nudge(
    nc: hp.NudgeConfig,
    nudge: float,
    direction: int,
    ts: float,
    last_ts: Optional[float],
    last_dir: int,
) -> tuple:
    """(new nudge, effect) for one warmer (+1) or colder (-1) signal."""
    if direction == 0:
        return nudge, EFFECT_NONE
    if last_ts is not None and direction == last_dir and 0.0 <= ts - last_ts < NUDGE_COLLAPSE_S:
        return nudge, EFFECT_NUDGE_COLLAPSED
    new = round(hp.clamp(nudge + direction * nc.step, nc.minimum, nc.maximum), 2)
    if new == nudge:
        return nudge, EFFECT_NUDGE_LIMIT
    return new, EFFECT_NUDGE
