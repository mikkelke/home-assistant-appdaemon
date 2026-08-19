"""
appliance_detectors.py - Shared evidence providers for the appliance FSM engine.

Every class here is a Detector (appliance_fsm.Detector): it watches sensors and emits typed Evidence
via ctx.emit(...). It NEVER calls a transition and NEVER holds a notifier - user-visible pushes go
through ctx.push_mobile (the engine's hypothesis-gated ActionSink), and all time/scheduling goes
through the injected ctx.now()/ctx.schedule()/ctx.cancel() seams. Config knobs are read from
ctx.config; required knobs are read with [] (a missing one is a config bug that must surface).

CONFIRM-TIMER OWNERSHIP (contract the dryer_policy continuation must honour):
  - PowerStartDetector owns the start-confirm timer AND the door-close fast-start window.
  - PowerEndDetector owns the finish-confirm (stop_for) timer.
  The transition table's "arm start-confirm" / "arm finish-confirm" / "arm door_fast_start" notes are
  DESCRIPTIVE; the matching policy actions must NOT schedule their own confirm timers, or the machine
  double-arms. Detectors gate on ctx.state (the authoritative internal state, F7), not on the HA
  entity, because a detector emits its arming evidence and the engine applies the store-only
  transition synchronously - so by the time a detector re-reads ctx.state it is already the target.

Emissions + knobs by detector:
  PowerStartDetector : POWER_HIGH, POWER_START_CONFIRMED   | start_w, run_for,
                       door_close_fast_confirm_s, door_close_fast_start_window_s, power_sensor, door_sensor
  PowerEndDetector   : POWER_LOW, POWER_END_CONFIRMED, POWER_RECOVERED | stop_w, stop_for, power_sensor
  DoorEdgeDetector   : DOOR_OPENED, DOOR_CLOSED (power snapshot in payload) | door_sensor, power_sensor,
                       door_sensor_inverted (optional)
  WatchdogTimer      : WD_RUNNING|WD_PAUSE|WD_UNEMPTIED|WD_EMPTIED | (duration, retry_delay_s at construction)
  PlugOutageDetector : PLUG_OUTAGE (+ ActionSink pushes) | power_sensor, power_unavailable_grace_s,
                       appliance_label (optional)
  BootRestoreProvider: BOOT_RESTORE (init only) | (snapshot handed in; reads no HA)

Pure stdlib, Python 3.12+, ASCII-only log/message strings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# Sibling-module import with the repo's defensive fallback (AppDaemon puts app dirs on sys.path, so
# a plain import normally resolves; the fallback covers any context where it has not - same shape as
# dryer_monitor.py:45-60 / washer_monitor.py's sibling imports).
try:
    from appliance_fsm import Detector, Evidence, EvidenceType, State
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from appliance_fsm import Detector, Evidence, EvidenceType, State


_UNAVAILABLE = ("unknown", "unavailable", None)


def _parse_watts(value: Any) -> Optional[float]:
    """A power reading as float, or None for a dropout (unknown/unavailable/None/garbage). None is
    the signal PlugOutageDetector owns - the power/end detectors ignore it rather than treat a
    dropout as 0W and false-finish a cycle (dryer_monitor.py:2051-2060)."""
    if value in _UNAVAILABLE:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _door_open(new: Any, ctx: Any) -> bool:
    """True when `new` means the door is OPEN. Standard contact is on=open; door_sensor_inverted
    flips it (U4 - dryer contact assumed standard unless the box says otherwise)."""
    return new == ("off" if ctx.config.get("door_sensor_inverted", False) else "on")


def _door_closed(new: Any, ctx: Any) -> bool:
    return new == ("on" if ctx.config.get("door_sensor_inverted", False) else "off")


class PowerStartDetector(Detector):
    """Start detection. Emits POWER_HIGH on each sample >= start_w and, once a start-confirm window
    (run_for, or door_close_fast_confirm_s inside the fast-start window) elapses with power still
    >= start_w, POWER_START_CONFIRMED. Owns the fast-start window: a door-close in OFF/EMPTIED arms
    it (a start seconds later confirms fast); a door-open clears it. A drop below start_w before the
    confirm fires aborts the pending start (dryer_monitor.py:1922-1942, 2065-2087)."""

    name = "power_start"

    def __init__(self) -> None:
        self._confirm: Any = None
        self._fast_until: Optional[datetime] = None

    def wire(self, ctx: Any) -> None:
        ctx.subscribe(ctx.config["power_sensor"], self.on_state)
        door = ctx.config.get("door_sensor")
        if door:
            ctx.subscribe(door, self.on_door)

    def on_state(self, entity: str, old: Any, new: Any, ctx: Any) -> None:
        watts = _parse_watts(new)
        if watts is None:
            return
        start_w = ctx.config["start_w"]
        if watts >= start_w:
            ctx.emit(Evidence.make(EvidenceType.POWER_HIGH, ctx.now(), self.name, payload={"watts": watts}))
            # ctx.state is already STARTING here (POWER_HIGH drove OFF->STARTING synchronously), so
            # STARTING is in the arming set alongside OFF.
            if ctx.state in (State.OFF, State.STARTING) and self._confirm is None:
                armed = self._fast_until is not None and ctx.now() <= self._fast_until
                delay = ctx.config["door_close_fast_confirm_s"] if armed else ctx.config["run_for"]
                self._confirm = ctx.schedule(delay, lambda: self._confirm_start(ctx))
        elif self._confirm is not None:
            ctx.cancel(self._confirm)
            self._confirm = None

    def _confirm_start(self, ctx: Any) -> None:
        self._confirm = None
        watts = _parse_watts(ctx.get_state(ctx.config["power_sensor"]))
        if watts is None:
            return  # plug dropout at confirm time - PlugOutageDetector owns it
        if watts >= ctx.config["start_w"]:
            self._fast_until = None
            ctx.emit(
                Evidence.make(EvidenceType.POWER_START_CONFIRMED, ctx.now(), self.name, payload={"watts": watts})
            )
        # else: stale/noise below start_w - no emit; the fast arm is preserved if still in window.

    def on_door(self, entity: str, old: Any, new: Any, ctx: Any) -> None:
        if _door_open(new, ctx):
            if ctx.state in (State.OFF, State.EMPTIED, State.STARTING):
                self._fast_until = None
        elif _door_closed(new, ctx):
            if ctx.state in (State.OFF, State.EMPTIED):
                self._fast_until = ctx.now() + timedelta(seconds=ctx.config["door_close_fast_start_window_s"])


class PowerEndDetector(Detector):
    """Finish detection. While RUNNING/ENDING: the first sample <= stop_w emits POWER_LOW and arms a
    stop_for confirm; a sample back above stop_w cancels it and emits POWER_RECOVERED. When the
    confirm fires with power still <= stop_w it emits POWER_END_CONFIRMED. Outside RUNNING/ENDING it
    drops any stale confirm (dryer_monitor.py:1943-1962, 2129-2191)."""

    name = "power_end"

    def __init__(self) -> None:
        self._end: Any = None

    def wire(self, ctx: Any) -> None:
        ctx.subscribe(ctx.config["power_sensor"], self.on_state)

    def on_state(self, entity: str, old: Any, new: Any, ctx: Any) -> None:
        watts = _parse_watts(new)
        if watts is None:
            return
        if ctx.state not in (State.RUNNING, State.ENDING):
            if self._end is not None:
                ctx.cancel(self._end)
                self._end = None
            return
        stop_w = ctx.config["stop_w"]
        if watts <= stop_w:
            if self._end is None:
                ctx.emit(Evidence.make(EvidenceType.POWER_LOW, ctx.now(), self.name, payload={"watts": watts}))
                self._end = ctx.schedule(ctx.config["stop_for"], lambda: self._confirm_end(ctx))
        elif self._end is not None:
            ctx.cancel(self._end)
            self._end = None
            ctx.emit(Evidence.make(EvidenceType.POWER_RECOVERED, ctx.now(), self.name, payload={"watts": watts}))

    def _confirm_end(self, ctx: Any) -> None:
        self._end = None
        watts = _parse_watts(ctx.get_state(ctx.config["power_sensor"]))
        if watts is None:
            return  # plug dropout at confirm time - PlugOutageDetector owns it
        if watts <= ctx.config["stop_w"]:
            ctx.emit(
                Evidence.make(EvidenceType.POWER_END_CONFIRMED, ctx.now(), self.name, payload={"watts": watts})
            )


class DoorEdgeDetector(Detector):
    """Splits a door-contact change into DOOR_OPENED / DOOR_CLOSED, each carrying a power snapshot
    ({"power_w"}). NO routing logic - which state a door edge moves the machine to is table data
    (dryer_monitor.py:1414-1421 emits; 1423-1512 routing becomes table rows). unknown/unavailable
    door values emit nothing."""

    name = "door_edge"

    def wire(self, ctx: Any) -> None:
        ctx.subscribe(ctx.config["door_sensor"], self.on_state)

    def on_state(self, entity: str, old: Any, new: Any, ctx: Any) -> None:
        watts = _parse_watts(ctx.get_state(ctx.config["power_sensor"]))
        payload = {"power_w": watts if watts is not None else 0.0}
        if _door_open(new, ctx):
            ctx.emit(Evidence.make(EvidenceType.DOOR_OPENED, ctx.now(), self.name, payload=payload))
        elif _door_closed(new, ctx):
            ctx.emit(Evidence.make(EvidenceType.DOOR_CLOSED, ctx.now(), self.name, payload=payload))


class WatchdogTimer(Detector):
    """Parameterized safety timer, kind in {running, pause, unemptied, emptied}. It does not wire or
    tick - the policy holds a reference and calls arm()/cancel()/arm_remaining() on state entry/exit
    (spec 4.2). When it fires it emits the matching WD_* Evidence via ctx.emit and inspects the
    returned SubmitResult: a LANDED fire (matched, not refused) or an UNMATCHED one (this evidence
    type has no row for the current state - the state has moved on) both clear the handle, done -
    a fired-but-late backstop hitting an unlisted pair is harmless, never a wedge. A REFUSED fire
    (the table row matched but the cooling gate blocked it - WD_RUNNING is RESPECT-cooling, spec
    3.3) instead reschedules itself `retry_delay_s` later rather than giving up: a watchdog that
    happens to fire inside a cooling window is not evidence the backstop is no longer needed, and
    silently abandoning it would leave that state with NO backstop for the rest of the episode -
    a real wedge, found via test_fsm_fuzz.py's restart-storm sweep (seed 84 reproduces it against
    an unpatched _fire). arm_remaining floors the delay at 60s so a restore never arms ~0 or
    negative seconds (dryer_monitor.py:634-639)."""

    _EVTYPE = {
        "running": EvidenceType.WD_RUNNING,
        "pause": EvidenceType.WD_PAUSE,
        "unemptied": EvidenceType.WD_UNEMPTIED,
        "emptied": EvidenceType.WD_EMPTIED,
    }

    def __init__(self, kind: str, duration_s: float, retry_delay_s: float = 90.0) -> None:
        if kind not in self._EVTYPE:
            raise ValueError(f"unknown watchdog kind {kind!r}")
        self.kind = kind
        self.name = f"watchdog_{kind}"
        self._evtype = self._EVTYPE[kind]
        self.duration_s = float(duration_s)
        # New, optional, defaulted - existing WatchdogTimer(kind, duration_s) call sites are
        # unaffected (backward compatible). Comfortably shorter than every cooling_period this
        # repo uses (300-600s) without being so short a still-refused retry spams the log.
        self.retry_delay_s = float(retry_delay_s)
        self._handle: Any = None

    @property
    def armed(self) -> bool:
        return self._handle is not None

    def arm(self, ctx: Any, duration_s: Optional[float] = None) -> Any:
        self.cancel(ctx)
        delay = self.duration_s if duration_s is None else float(duration_s)
        self._handle = ctx.schedule(delay, lambda: self._fire(ctx))
        return self._handle

    def arm_remaining(self, ctx: Any, since: datetime, floor_s: float = 60.0) -> Any:
        remaining = max(float(floor_s), self.duration_s - (ctx.now() - since).total_seconds())
        self.cancel(ctx)
        self._handle = ctx.schedule(remaining, lambda: self._fire(ctx))
        return self._handle

    def cancel(self, ctx: Any) -> None:
        if self._handle is not None:
            ctx.cancel(self._handle)
            self._handle = None

    def _fire(self, ctx: Any) -> None:
        self._handle = None
        result = ctx.emit(Evidence.make(self._evtype, ctx.now(), self.name))
        if result.refused:
            self._handle = ctx.schedule(self.retry_delay_s, lambda: self._fire(ctx))


class PlugOutageDetector(Detector):
    """Tolerates a short power-sensor dropout (HA restart, ESPHome OTA) for power_unavailable_grace_s,
    then declares an outage: it emits PLUG_OUTAGE (the table wipes an in-progress cycle to Off) and
    pages the phone once. On the next valid reading after an outage it pages recovery. It never
    force-changes state itself - the state wipe is a table row (dryer_monitor.py:2193-2269)."""

    name = "plug_outage"

    def __init__(self) -> None:
        self._grace: Any = None
        self._outaged = False

    def wire(self, ctx: Any) -> None:
        ctx.subscribe(ctx.config["power_sensor"], self.on_state)

    def _label(self, ctx: Any) -> str:
        return ctx.config.get("appliance_label", "appliance")

    def on_state(self, entity: str, old: Any, new: Any, ctx: Any) -> None:
        if _parse_watts(new) is None:
            if self._grace is None and not self._outaged:
                self._grace = ctx.schedule(ctx.config["power_unavailable_grace_s"], lambda: self._elapsed(ctx))
        else:
            if self._grace is not None:
                ctx.cancel(self._grace)
                self._grace = None
            if self._outaged:
                self._outaged = False
                ctx.push_mobile(f"Power plug is reporting again - {self._label(ctx)} monitoring resumed.")

    def _elapsed(self, ctx: Any) -> None:
        self._grace = None
        if _parse_watts(ctx.get_state(ctx.config["power_sensor"])) is not None:
            return  # recovered before the grace expired
        self._outaged = True
        ctx.emit(Evidence.make(EvidenceType.PLUG_OUTAGE, ctx.now(), self.name))
        grace = int(ctx.config["power_unavailable_grace_s"])
        ctx.push_mobile(
            f"Power plug stopped reporting (unavailable >= {grace}s) - cycle monitoring is blind and "
            f"the {self._label(ctx)} just looks Off. Check the plug/WiFi."
        )


class BootRestoreProvider(Detector):
    """Packages a boot snapshot - handed to it, NOT read from HA - into exactly one BOOT_RESTORE
    Evidence (live=False) at init. Precedence between entity/store/helper snapshots is the policy's
    job (spec 4.3); this provider only wraps the resolved snapshot. restore() returns None for an
    empty/None snapshot (nothing to restore). The engine consumes the Evidence via
    ApplianceFSM.apply_boot_restore, never via submit()."""

    name = "boot_restore"

    def __init__(self, snapshot: Optional[dict] = None, source: str = "unknown") -> None:
        self._snapshot = snapshot
        self._source = source

    def set_snapshot(self, snapshot: Optional[dict], source: Optional[str] = None) -> None:
        self._snapshot = snapshot
        if source is not None:
            self._source = source

    def restore(self, boot_ctx: Any) -> Optional[Evidence]:
        snap = self._snapshot
        if not snap:
            return None
        payload = dict(snap)
        payload.setdefault("source", self._source)
        return Evidence.make(EvidenceType.BOOT_RESTORE, boot_ctx.now(), self.name, live=False, payload=payload)
