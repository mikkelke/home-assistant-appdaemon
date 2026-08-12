"""
Shared precedence for the one blind that several apps command.

``cover.bedroom_blind`` has three writers, and before this module each of them
called ``cover/set_cover_position`` on its own:

  * ``bedroom_blind_control``  - the wall remote's hold gestures (manual press)
  * ``wakeup_bedroom``         - the morning open, at the wake target or the
                                 venting default
  * ``bedroom_solar_shade``    - daylight/heat balancing through the day

Nothing expressed how they rank, so whoever wrote last won, and each carried its
own private notion of "somebody moved it by hand". This module holds the order as
plain data, in one place, testable without an AppDaemon runtime:

    manual wall press  >  venting need  >  wake target  >  solar shade

Why that order:

* **manual** - a hand on the wall switch is a person in the room saying what they
  want right now. Nothing automatic outranks it.
* **venting** - when the night's cooling plan is "open the window", the blind must
  land where the openable pane is actually clear. Shading a room we intend to
  ventilate trades away the cooling method in use for the one that is not
  (incident 2026-08-09: the plan said "windows", the wake routine parked the blind
  at the heat-block position, the window could not be opened at all, and the sealed
  dark room then also kept the lights on until 08:44).
* **wake** - the morning open is a scheduled, once-a-day event with a light ramp
  waiting on it; it must beat the continuous background balancing.
* **shade** - the lowest rank on purpose. It re-evaluates every few minutes, so it
  is the one writer that can quietly undo any of the others.

POSITIONS ARE INVERTED on this cover - read ``bedroom_solar_shade``'s banner before
reasoning about any number here. This module never interprets a position; it only
decides *whose* number is used.

Deliberately dependency-free (no ``appdaemon`` import, no I/O): every app can import
it, and a failure to import can be caught and degraded past without taking the app
down with it. ``command()`` is the only function that touches Home Assistant, and it
takes the Hass instance as its first argument, mirroring ``cover_util`` /
``lighting_actions`` / ``room_state_darkness``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

SOURCE_MANUAL = "manual"
SOURCE_VENT = "vent"
SOURCE_WAKE = "wake"
SOURCE_SHADE = "shade"

#: Higher number = higher precedence. An unknown source ranks below every known
#: one (0) rather than raising: a new caller that forgets to register itself
#: should lose an argument, not crash the app that asked.
PRIORITIES: dict[str, int] = {
    SOURCE_MANUAL: 40,
    SOURCE_VENT: 30,
    SOURCE_WAKE: 20,
    SOURCE_SHADE: 10,
}

#: The order as documented, highest first - handy for logs and tests.
PRECEDENCE = (SOURCE_MANUAL, SOURCE_VENT, SOURCE_WAKE, SOURCE_SHADE)


@dataclass(frozen=True)
class BlindRequest:
    """One source's claim on the blind.

    ``position`` is the position that source wants (device frame). ``active``
    is the caller's own answer to "does this source have an opinion right now" -
    a manual pause that has expired, a wake window that has closed, or a venting
    plan that is not in effect all pass ``active=False`` and are simply skipped.
    A request with ``position=None`` is a veto-shaped claim: it can outrank and
    block a lower source without proposing a number of its own.
    """

    source: str
    position: int | None = None
    reason: str = ""
    active: bool = True

    @property
    def priority(self) -> int:
        return priority(self.source)


def priority(source: str | None) -> int:
    """Precedence of ``source``; 0 for anything unregistered."""
    if not source:
        return 0
    return PRIORITIES.get(str(source), 0)


def _live(requests: Iterable[BlindRequest] | None) -> list[BlindRequest]:
    return [r for r in (requests or []) if r is not None and r.active]


def top(requests: Iterable[BlindRequest] | None) -> BlindRequest | None:
    """Highest-priority active request, position or not.

    Ties keep the order the caller listed them in (``max`` is stable), so a
    caller can express "mine first" among equals without inventing a rank.
    """
    live = _live(requests)
    if not live:
        return None
    return max(live, key=lambda r: r.priority)


def arbitrate(requests: Iterable[BlindRequest] | None) -> BlindRequest | None:
    """The request that gets to move the blind, or ``None`` if nobody does.

    A higher-priority request with no position of its own (``position=None``)
    blocks everything below it: that is how "the wake routine owns the blind
    right now" or "paused after a manual move" is expressed without the blocker
    having to know where the blind should sit.
    """
    winner = top(requests)
    if winner is None or winner.position is None:
        return None
    return winner


def may_write(source: str, requests: Iterable[BlindRequest] | None) -> bool:
    """True when ``source`` is the one the arbitration picked."""
    winner = arbitrate(requests)
    return winner is not None and winner.source == source


def blocked_by(source: str, requests: Iterable[BlindRequest] | None) -> BlindRequest | None:
    """The request outranking ``source``, for logging why a write was skipped."""
    winner = top(requests)
    if winner is None or winner.source == source:
        return None
    if winner.priority <= priority(source):
        return None
    return winner


def describe(requests: Iterable[BlindRequest] | None) -> str:
    """One-line rendering of the whole claim set, for a log that has to be read
    at 06:00 by someone wondering why the blind did what it did."""
    parts = []
    for r in sorted(_live(requests), key=lambda r: -r.priority):
        pos = "-" if r.position is None else str(r.position)
        parts.append(f"{r.source}={pos}" + (f" ({r.reason})" if r.reason else ""))
    return ", ".join(parts) if parts else "no active requests"


def command(
    hass: Any,
    entity: str,
    position: int,
    *,
    source: str = "",
    reason: str = "",
    log_fn: Callable[[str], None] | None = None,
) -> int:
    """The single place a blind position is actually written.

    Every writer routing through here means a future owner app only has to
    replace this function body, not hunt down four call sites. Issues exactly the
    service call the callers used to make themselves, so behaviour and any
    mock-based test assertions are unchanged.
    """
    pos = int(position)
    hass.call_service("cover/set_cover_position", entity_id=entity, position=pos)
    if log_fn:
        tag = f" [{source}]" if source else ""
        why = f" ({reason})" if reason else ""
        log_fn(f"Set {entity} -> {pos}%{tag}{why}")
    return pos
