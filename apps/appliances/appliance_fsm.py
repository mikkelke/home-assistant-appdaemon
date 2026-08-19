"""
appliance_fsm.py - Shared appliance state-machine ENGINE (core only, no appliance physics).

Data-driven interpreter the dryer/washer/dishwasher policies run on. It owns four invariants that
were hand-copied per call site before (each omission caused a real incident class):

  1. ONE cooling gate keyed by EVENT CLASS, logging every outcome - taken OR refused; no path
     silently swallows a physical transition (34h wedge, dryer_monitor.py:1498-1506). See
     _cooling_respects + the REFUSED INFO log.
  2. EXACTLY-ONCE feedback: save only when a transition lands AND the cycle_id was not already fed
     back (nine junk records, 2026-08-12, F3). See _request_feedback.
  3. HYPOTHESIS gate: a restored-uncorroborated state cannot notify - announce/push refused
     unconditionally at the engine boundary (washer 2026-08-19, F2). See _user_action.
  4. Time/scheduling ONLY via injected Clock/Scheduler seams (R4); the engine never calls
     datetime.now or an AppDaemon timer, so fuzz/replay stay deterministic.

Appliance-specific behaviour (thresholds, classification, the transition TABLE, guard/action bodies)
lives in a POLICY object; the engine resolves guards/actions BY NAME against it. The host (shadow
app) supplies the I/O seams (get_state, publish, scheduler, ActionSink).

Policy contract (duck-typed, validated at construction):
  policy.table   : dict[State, dict[EvidenceType, list[Row | dict]]]  - transition table (data).
  policy.guards  : Mapping[str, Callable[[Ctx], bool]]                - named pure predicates.
  policy.actions : Mapping[str, Callable[[Ctx], None]]               - named side-effecting actions.
An action runs with ctx.state == the FROM state (the engine applies the target AFTER it returns,
per the pipeline lookup->guards->cooling->action->publish). It may call ctx.announce/push_mobile/
select_option/reset_selectors (announce/push hypothesis-gated), ctx.request_feedback (exactly-once),
ctx.mint_cycle_id/set_hypothesis, ctx.schedule. Pure stdlib, Python 3.12+, ASCII-only logs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

# --- Internal state model + HA-published vocabulary (spec section 2) ---


class State(Enum):
    """Engine-internal states. STARTING/ENDING are transient confirm windows mapping to an
    already-visible published value (Off / Running) - see PUBLISHED and RESTORE_STATE_OF."""

    OFF = "OFF"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ENDING = "ENDING"
    FINISHED = "FINISHED"
    EMPTIED = "EMPTIED"


# The ONLY vocabulary HA ever sees. STARTING stays Off (publishing Running would flap Running<->Off
# on every non-confirming blip and guarantee a per-cycle shadow divergence); ENDING stays Running
# for the same reason (spec section 2, R3).
PUBLISHED: dict[State, str] = {
    State.OFF: "Off",
    State.STARTING: "Off",
    State.RUNNING: "Running",
    State.PAUSED: "Paused",
    State.ENDING: "Running",
    State.FINISHED: "Unemptied",
    State.EMPTIED: "Emptied",
}

# Boot-restore inverse: the persisted published subset maps 1:1 back to an internal state
# (STARTING/ENDING never persist). Unknown -> OFF, the safe idle default.
RESTORE_STATE_OF: dict[str, State] = {
    "Off": State.OFF,
    "Running": State.RUNNING,
    "Paused": State.PAUSED,
    "Unemptied": State.FINISHED,
    "Emptied": State.EMPTIED,
}


# --- Typed evidence + event classes (spec 3.1-3.2) ---


class EventClass(Enum):
    """Drives the cooling matrix and the boot truth-hierarchy (door/physical > power > clock >
    stored). Fixed per EvidenceType via EVENT_CLASS_OF - a detector never picks the class."""

    POWER = "POWER"
    PHYSICAL = "PHYSICAL"
    WATCHDOG = "WATCHDOG"
    CLOCK = "CLOCK"
    RESTORE = "RESTORE"


class EvidenceType(Enum):
    POWER_HIGH = "POWER_HIGH"
    POWER_START_CONFIRMED = "POWER_START_CONFIRMED"
    POWER_LOW = "POWER_LOW"
    POWER_END_CONFIRMED = "POWER_END_CONFIRMED"
    POWER_RECOVERED = "POWER_RECOVERED"
    KEEP_FRESH = "KEEP_FRESH"
    PLUG_OUTAGE = "PLUG_OUTAGE"
    DOOR_OPENED = "DOOR_OPENED"
    DOOR_CLOSED = "DOOR_CLOSED"
    FORCE_EMPTIED = "FORCE_EMPTIED"
    WD_RUNNING = "WD_RUNNING"
    WD_PAUSE = "WD_PAUSE"
    WD_UNEMPTIED = "WD_UNEMPTIED"
    WD_EMPTIED = "WD_EMPTIED"
    RECONCILE = "RECONCILE"
    BOOT_RESTORE = "BOOT_RESTORE"


# Fixed EvidenceType -> EventClass (spec 3.2). PLUG_OUTAGE is POWER-class (respects cooling like any
# power signal); RECONCILE is CLOCK; BOOT_RESTORE is RESTORE, consumed via apply_boot_restore(), not
# submit().
EVENT_CLASS_OF: dict[EvidenceType, EventClass] = {
    EvidenceType.POWER_HIGH: EventClass.POWER,
    EvidenceType.POWER_START_CONFIRMED: EventClass.POWER,
    EvidenceType.POWER_LOW: EventClass.POWER,
    EvidenceType.POWER_END_CONFIRMED: EventClass.POWER,
    EvidenceType.POWER_RECOVERED: EventClass.POWER,
    EvidenceType.KEEP_FRESH: EventClass.POWER,
    EvidenceType.PLUG_OUTAGE: EventClass.POWER,
    EvidenceType.DOOR_OPENED: EventClass.PHYSICAL,
    EvidenceType.DOOR_CLOSED: EventClass.PHYSICAL,
    EvidenceType.FORCE_EMPTIED: EventClass.PHYSICAL,
    EvidenceType.WD_RUNNING: EventClass.WATCHDOG,
    EvidenceType.WD_PAUSE: EventClass.WATCHDOG,
    EvidenceType.WD_UNEMPTIED: EventClass.WATCHDOG,
    EvidenceType.WD_EMPTIED: EventClass.WATCHDOG,
    EvidenceType.RECONCILE: EventClass.CLOCK,
    EvidenceType.BOOT_RESTORE: EventClass.RESTORE,
}


@dataclass(frozen=True)
class Evidence:
    """One typed fact. `ts` is aware UTC from the injected Clock (never datetime.now). `live`
    distinguishes a real signal this tick (True) from a hypothesis/replay/restore (False). `payload`
    is typed-by-convention, not enforced: POWER_* -> {"watts"}; KEEP_FRESH -> {"mean","peak","stdev"};
    DOOR_* -> {"power_w"}; WD_* -> {}; BOOT_RESTORE -> {"state","start_time","energy_at_start",
    "detected_programme","programme_duration_min","state_since","last_high_energy_at",
    "notification_sent","cycle_id","code_fingerprint","source"}."""

    type: EvidenceType
    event_class: EventClass
    ts: datetime
    source: str
    live: bool
    payload: dict = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        type: EvidenceType,
        ts: datetime,
        source: str,
        live: bool = True,
        payload: Optional[dict] = None,
    ) -> "Evidence":
        """Detector-facing factory: fills event_class from EVENT_CLASS_OF. `ts` must be ctx.now()."""
        return cls(type, EVENT_CLASS_OF[type], ts, source, live, dict(payload or {}))


# --- Injectable seams (spec 10.1) + the ActionSink protocol ---


class Clock(Protocol):
    """The only source of 'now'. Returns an aware UTC datetime."""

    def now(self) -> datetime: ...


class Scheduler(Protocol):
    """The only way to schedule work. Callbacks are ZERO-ARG (the host adapter absorbs AppDaemon's
    kwargs). run_in returns an opaque handle for cancel; cancel of a stale handle is a harmless no-op."""

    def run_in(self, delay_s: float, cb: Callable[[], None]) -> Any: ...

    def cancel(self, handle: Any) -> None: ...


class SystemClock:
    """Production Clock: the one sanctioned datetime.now() call. Injected, never reached by engine
    logic, so swapping in a FakeClock keeps the engine deterministic."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ActionSink(Protocol):
    """The complete user-visible action surface. announce/push_mobile are hypothesis-gated by the
    engine; select_option/reset_selectors mirror UI helpers (not gated); save_feedback is invoked
    ONLY by the engine's exactly-once path, never directly by policy. A shadow ActionSink implements
    each method as an inert recorder with no notifier handle, so the shadow cannot notify even if the
    engine tried."""

    def announce(self, message: str, **kwargs: Any) -> None: ...
    def push_mobile(self, message: str, **kwargs: Any) -> None: ...
    def select_option(self, entity: str, option: str, **kwargs: Any) -> None: ...
    def reset_selectors(self, **kwargs: Any) -> None: ...
    def save_feedback(self, record: dict) -> None: ...


# Cooling override tokens a Row may carry to force a per-row decision over the class default.
RESPECT = "RESPECT"
BYPASS = "BYPASS"


# --- Transition table: data literal -> validated interpreter ---


@dataclass(frozen=True)
class Row:
    """One transition candidate. `guards` all must pass for the row to fire; first matching row (in
    order) wins. `action` names a policy action (or None). `target` is the internal state to move to
    (None = stay: an attr-only update or a documented no-op). `cooling` forces RESPECT/BYPASS over
    the event-class default - needed where a POWER evidence routes to a physical outcome that must
    not be swallowed (e.g. POWER_END_CONFIRMED -> EMPTIED via a door edge)."""

    guards: tuple[str, ...] = ()
    action: Optional[str] = None
    target: Optional[State] = None
    cooling: Optional[str] = None


def _as_row(raw: Any) -> Row:
    if isinstance(raw, Row):
        return raw
    if isinstance(raw, Mapping):
        return Row(
            guards=tuple(raw.get("guards", ()) or ()),
            action=raw.get("action"),
            target=raw.get("target"),
            cooling=raw.get("cooling"),
        )
    raise TypeError(f"transition row must be a Row or dict, got {type(raw).__name__}")


class TransitionTable:
    """Normalizes + validates a policy's raw transition literal, then answers resolve(state, evtype).
    Validation is fail-fast at construction: right enum keys, State/None targets, RESPECT/BYPASS/None
    cooling, and every guard/action NAME present in the policy namespaces - so a table typo is caught
    at app start, not as a silent runtime no-op."""

    def __init__(
        self,
        raw: Mapping[State, Mapping[EvidenceType, Sequence[Any]]],
        guard_names: Sequence[str],
        action_names: Sequence[str],
    ) -> None:
        guards, actions = set(guard_names), set(action_names)
        errors: list[str] = []
        table: dict[State, dict[EvidenceType, tuple[Row, ...]]] = {}
        for state, by_ev in raw.items():
            if not isinstance(state, State):
                errors.append(f"table key {state!r} is not a State")
                continue
            table[state] = {}
            for evtype, rows in by_ev.items():
                if not isinstance(evtype, EvidenceType):
                    errors.append(f"{state.name}: key {evtype!r} is not an EvidenceType")
                    continue
                norm: list[Row] = []
                for raw_row in rows:
                    row = _as_row(raw_row)
                    if row.target is not None and not isinstance(row.target, State):
                        errors.append(f"{state.name}/{evtype.name}: target {row.target!r} not a State")
                    if row.cooling not in (None, RESPECT, BYPASS):
                        errors.append(f"{state.name}/{evtype.name}: cooling {row.cooling!r} invalid")
                    for g in row.guards:
                        if g not in guards:
                            errors.append(f"{state.name}/{evtype.name}: unknown guard {g!r}")
                    if row.action is not None and row.action not in actions:
                        errors.append(f"{state.name}/{evtype.name}: unknown action {row.action!r}")
                    norm.append(row)
                table[state][evtype] = tuple(norm)
        if errors:
            raise ValueError("invalid transition table: " + "; ".join(errors))
        self._table = table

    def resolve(self, state: State, evtype: EvidenceType) -> tuple[Row, ...]:
        return self._table.get(state, {}).get(evtype, ())


# --- Context for detectors, guards and actions (spec 5.1) ---


class Ctx:
    """The single object detectors, guards and actions touch. It bundles the injected seams and a
    read-only view of engine state; it never exposes a transition method or a raw notifier.
    Detectors call emit(); guards read state/evidence/get_state; actions additionally use the gated
    user actions, request_feedback, and the cycle-id/hypothesis helpers. `evidence` is the fact being
    resolved (None outside submit())."""

    def __init__(
        self,
        fsm: "ApplianceFSM",
        *,
        clock: Clock,
        scheduler: Scheduler,
        log: Callable[..., None],
        config: Optional[Mapping[str, Any]],
        get_state: Optional[Callable[..., Any]],
        get_history: Optional[Callable[..., Any]],
        subscribe: Optional[Callable[[str, Callable], None]],
    ) -> None:
        self._fsm = fsm
        self._clock = clock
        self._scheduler = scheduler
        self._log = log
        self.config = dict(config or {})
        self._get_state = get_state
        self._get_history = get_history
        self._subscribe = subscribe
        self._evidence: Optional[Evidence] = None

    # injected time + scheduling seams (never datetime.now / run_in directly)
    def now(self) -> datetime:
        return self._clock.now()

    def schedule(self, delay_s: float, cb: Callable[[], None]) -> Any:
        return self._scheduler.run_in(delay_s, cb)

    def cancel(self, handle: Any) -> None:
        if handle is not None:
            self._scheduler.cancel(handle)

    # read-only HA access (recorder history is the durable arbiter for door edges, F7)
    def get_state(self, entity: str, **kwargs: Any) -> Any:
        return self._get_state(entity, **kwargs) if self._get_state else None

    def get_history(self, entity: str, **kwargs: Any) -> Any:
        return self._get_history(entity, **kwargs) if self._get_history else None

    # wiring: host binds `handler` to listen_state on `entity`; handler(entity, old, new, ctx)
    def subscribe(self, entity: str, handler: Callable[..., None]) -> None:
        if self._subscribe:
            self._subscribe(entity, handler)

    # evidence emission (the ONLY way a detector reaches the engine)
    def emit(self, evidence: Evidence) -> "SubmitResult":
        return self._fsm.submit(evidence)

    # read-only engine view
    @property
    def state(self) -> State:
        return self._fsm.state

    @property
    def evidence(self) -> Optional[Evidence]:
        return self._evidence

    @property
    def hypothesis(self) -> bool:
        return self._fsm.hypothesis

    @property
    def cycle_id(self) -> Optional[str]:
        return self._fsm.cycle_id

    # cycle identity + hypothesis (engine-owned; policy drives them from actions)
    def mint_cycle_id(self) -> str:
        return self._fsm.mint_cycle_id()

    def set_cycle_id(self, cid: Optional[str]) -> None:
        self._fsm.set_cycle_id(cid)

    def set_hypothesis(self, value: bool) -> None:
        self._fsm.set_hypothesis(value)

    def clear_hypothesis(self) -> None:
        self._fsm.set_hypothesis(False)

    # feedback: policy hands a record, engine enforces exactly-once (spec section 6)
    def request_feedback(self, record: dict) -> None:
        self._fsm._request_feedback(record)

    # user actions: announce/push refused while hypothesis is True (spec section 8)
    def announce(self, message: str, **kwargs: Any) -> None:
        self._fsm._user_action("announce", (message,), kwargs)

    def push_mobile(self, message: str, **kwargs: Any) -> None:
        self._fsm._user_action("push_mobile", (message,), kwargs)

    def select_option(self, entity: str, option: str, **kwargs: Any) -> None:
        self._fsm._user_action("select_option", (entity, option), kwargs)

    def reset_selectors(self, **kwargs: Any) -> None:
        self._fsm._user_action("reset_selectors", (), kwargs)

    def log(self, message: str, level: str = "INFO") -> None:
        self._log(message, level=level)


class Detector:
    """Base for every evidence provider (spec 5.1). Subscribe entities in wire(via ctx.subscribe);
    react in on_state(entity, old, new, ctx) / on_event(name, data, ctx) / tick(ctx); emit typed
    Evidence via ctx.emit(...). Never call a transition, never hold a notifier. restore(boot_ctx)
    returns at most one Evidence from a boot snapshot handed to the detector (it does not read HA)."""

    name = "detector"

    def wire(self, ctx: Ctx) -> None: ...
    def on_state(self, entity: str, old: Any, new: Any, ctx: Ctx) -> None: ...
    def on_event(self, name: str, data: dict, ctx: Ctx) -> None: ...
    def tick(self, ctx: Ctx) -> None: ...
    def restore(self, boot_ctx: Ctx) -> Optional[Evidence]:
        return None


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of one submit(), for tests and shadow divergence bookkeeping."""

    from_state: State
    to_state: State
    published: str
    matched: bool = False
    refused: bool = False
    action: Optional[str] = None
    published_changed: bool = False
    store_only: bool = False


# --- The engine ---


class ApplianceFSM:
    """Data-driven appliance state machine. Construct with a policy (table + named guards/actions)
    and the I/O seams; feed typed Evidence via submit(). See the module docstring for the policy
    contract and the four invariants.

    submit(evidence): resolve rows for (state, type) -> first row whose guards all pass -> if the
    mapped published value would change, consult the cooling gate (REFUSED at INFO on a block) ->
    dispatch the action -> apply the state change and publish (store-only, no HA re-publish, when the
    published value is unchanged)."""

    def __init__(
        self,
        policy: Any,
        actions: ActionSink,
        clock: Clock,
        scheduler: Scheduler,
        publish: Callable[..., None],
        log: Callable[..., None],
        *,
        config: Optional[Mapping[str, Any]] = None,
        get_state: Optional[Callable[..., Any]] = None,
        get_history: Optional[Callable[..., Any]] = None,
        subscribe: Optional[Callable[[str, Callable], None]] = None,
        detectors: Sequence[Detector] = (),
        cooling_period: float = 600.0,
        initial_state: State = State.OFF,
    ) -> None:
        self._policy = policy
        self._actions = actions
        self._clock = clock
        self._scheduler = scheduler
        # publish(published, *, internal, store_only, attrs): host writes the state entity (and,
        # unless store_only, mirrors to HA); the engine never calls set_state itself.
        self._publish = publish
        self._log = log
        self._cooling_period = float(cooling_period)
        self._detectors = list(detectors)

        self._guards: Mapping[str, Callable[[Ctx], bool]] = dict(getattr(policy, "guards", {}) or {})
        self._actions_map: Mapping[str, Callable[[Ctx], None]] = dict(getattr(policy, "actions", {}) or {})
        self._table = TransitionTable(
            getattr(policy, "table", {}) or {}, list(self._guards), list(self._actions_map)
        )

        self._state = initial_state
        self._published = PUBLISHED[initial_state]
        self._state_since: Optional[datetime] = None
        # None, not now(): never seed the cooling clock at boot, or the first post-restart transition
        # is swallowed (dryer_monitor.py:88-94).
        self._last_state_change: Optional[datetime] = None
        self._hypothesis = False
        self._cycle_id: Optional[str] = None
        self._last_feedback_cycle_id: Optional[str] = None
        self._tick_handle: Any = None

        self._ctx = Ctx(
            self,
            clock=clock,
            scheduler=scheduler,
            log=log,
            config=config,
            get_state=get_state,
            get_history=get_history,
            subscribe=subscribe,
        )

    @property
    def state(self) -> State:
        return self._state

    @property
    def published(self) -> str:
        return self._published

    @property
    def state_since(self) -> Optional[datetime]:
        return self._state_since

    @property
    def last_state_change(self) -> Optional[datetime]:
        return self._last_state_change

    @property
    def hypothesis(self) -> bool:
        return self._hypothesis

    @property
    def cycle_id(self) -> Optional[str]:
        return self._cycle_id

    @property
    def ctx(self) -> Ctx:
        return self._ctx

    def _now(self) -> datetime:
        return self._clock.now()

    def submit(self, evidence: Evidence) -> SubmitResult:
        state = self._state
        rows = self._table.resolve(state, evidence.type)
        prev_ev = self._ctx._evidence
        self._ctx._evidence = evidence
        try:
            if not rows:
                self._log(f"no-op ({state.name}/{evidence.type.name}): no row in table", level="DEBUG")
                return SubmitResult(state, state, self._published)
            row = self._match(rows)
            if row is None:
                self._log(
                    f"no-op ({state.name}/{evidence.type.name}): no guard combo matched", level="DEBUG"
                )
                return SubmitResult(state, state, self._published)

            target = row.target if row.target is not None else state
            from_pub, to_pub = PUBLISHED[state], PUBLISHED[target]
            published_change = from_pub != to_pub

            if published_change and self._refused_by_cooling(row, evidence, state, target):
                return SubmitResult(
                    state, target, self._published, matched=True, refused=True, action=row.action
                )

            acted = False
            if row.action:
                self._actions_map[row.action](self._ctx)
                acted = True

            moved = target != state
            store_only = False
            if moved:
                self._state = target
                self._state_since = self._now()
                if published_change:
                    self._last_state_change = self._now()
                    self._published = to_pub
                    self._emit_publish(store_only=False)
                else:
                    # Same published value (OFF<->STARTING, RUNNING<->ENDING): persist the store but
                    # do NOT re-publish to HA - dodges the set_state attribute-strip risk and keeps
                    # last_changed meaningful (spec section 2, U1).
                    store_only = True
                    self._emit_publish(store_only=True)

            if not acted and not moved:
                self._log(
                    f"no-op ({state.name}/{evidence.type.name}): explicit no-op row", level="DEBUG"
                )

            return SubmitResult(
                state,
                target,
                self._published,
                matched=True,
                action=row.action,
                published_changed=(moved and published_change),
                store_only=store_only,
            )
        finally:
            self._ctx._evidence = prev_ev

    def _match(self, rows: Sequence[Row]) -> Optional[Row]:
        for row in rows:
            if all(self._guards[g](self._ctx) for g in row.guards):
                return row
        return None

    # cooling matrix (spec 3.3): the single gate that replaces every scattered force=True
    def _cooling_respects(self, row: Row, evidence: Evidence) -> bool:
        """True = cooling may block; False = bypass. Row override wins; else by event class: PHYSICAL
        and CLOCK(RECONCILE) bypass; POWER respects (cooling damps power oscillation); WATCHDOG
        bypasses EXCEPT WD_RUNNING (kept respectful to match the live app); RESTORE never reaches here."""
        if row.cooling == RESPECT:
            return True
        if row.cooling == BYPASS:
            return False
        ec = evidence.event_class
        if ec in (EventClass.PHYSICAL, EventClass.CLOCK, EventClass.RESTORE):
            return False
        if ec == EventClass.WATCHDOG:
            return evidence.type == EvidenceType.WD_RUNNING
        return True  # POWER (incl. PLUG_OUTAGE)

    def _refused_by_cooling(self, row: Row, evidence: Evidence, state: State, target: State) -> bool:
        if not self._cooling_respects(row, evidence):
            return False
        if self._last_state_change is None:
            return False
        # UTC, aware, from the injected Clock - DST fall-back lesson (dryer_monitor.py:1386-1397): a
        # naive delta goes negative for the repeated hour and refuses everything.
        elapsed = (self._now() - self._last_state_change).total_seconds()
        if elapsed < self._cooling_period:
            self._log(
                f"REFUSED {state.name}->{target.name} "
                f"({evidence.type.name}/{evidence.event_class.name}): "
                f"cooling {int(elapsed)}s < {int(self._cooling_period)}s",
                level="INFO",
            )
            return True
        return False

    def _emit_publish(self, *, store_only: bool) -> None:
        self._publish(self._published, internal=self._state, store_only=store_only, attrs=self._engine_attrs())

    def _engine_attrs(self) -> dict:
        """Engine-owned state-entity attributes; the host merges these with policy attrs (and omits
        hypothesis when False, per the AD 0/False-drop rule)."""
        return {"internal_state": self._state.name, "hypothesis": self._hypothesis, "cycle_id": self._cycle_id}

    # exactly-once feedback (spec section 6)
    def _request_feedback(self, record: dict) -> None:
        """Called from a landing FINISHED action via ctx.request_feedback. Saves iff a cycle_id is
        set AND not already fed back, then stamps it - so a re-entered finish path (nine junk
        records, 2026-08-12, F3) can never save twice for one cycle."""
        cid = self._cycle_id
        if cid is not None and cid != self._last_feedback_cycle_id:
            self._actions.save_feedback(record)
            self._last_feedback_cycle_id = cid
        else:
            self._log(f"feedback suppressed: cycle_id {cid} already saved or unset", level="DEBUG")

    # hypothesis gate (spec section 8): the belt. The shadow's no-notifier wire is the suspenders.
    def _user_action(self, kind: str, args: tuple, kwargs: dict) -> None:
        if kind in ("announce", "push_mobile") and self._hypothesis:
            self._log(
                f"REFUSED {kind}: hypothesis state (restored, uncorroborated) cannot notify",
                level="INFO",
            )
            return
        getattr(self._actions, kind)(*args, **kwargs)

    # cycle identity (spec section 6): minted only on a genuine start, carried across restarts
    def mint_cycle_id(self) -> str:
        self._cycle_id = uuid.uuid4().hex
        self._last_feedback_cycle_id = None
        return self._cycle_id

    def set_cycle_id(self, cid: Optional[str]) -> None:
        self._cycle_id = cid

    def mark_fed_back(self, cycle_id: Optional[str]) -> None:
        """Restore the exactly-once guard after a restart. The in-memory guard does not survive a
        process restart, so for cross-restart exactly-once the policy must persist last_feedback
        alongside cycle_id and replay it here (with set_cycle_id) - otherwise a re-derived FINISH
        (e.g. boot self-heal) could save a second record for an already-fed-back cycle."""
        self._last_feedback_cycle_id = cycle_id

    def set_hypothesis(self, value: bool) -> None:
        self._hypothesis = bool(value)

    # boot restore (spec 3.3/4.3): set state directly, never through cooling, never an action. The
    # precedence + corroboration decision is the policy's; it passes the resulting hypothesis.
    def enter_state_silently(
        self,
        state: State,
        *,
        state_since: Optional[datetime] = None,
        hypothesis: bool = False,
        cycle_id: Optional[str] = None,
    ) -> None:
        self._state = state
        self._state_since = state_since or self._now()
        self._published = PUBLISHED[state]
        self._hypothesis = bool(hypothesis)
        if cycle_id is not None:
            self._cycle_id = cycle_id
        # last_state_change left as-is (None at boot): see __init__.
        self._emit_publish(store_only=False)

    def apply_boot_restore(self, evidence: Evidence, *, hypothesis: bool = False) -> State:
        """Consume a BOOT_RESTORE Evidence: map its published `state` back to internal and enter it
        silently. The policy computes `hypothesis` from corroboration and re-arms watchdogs
        separately; this only sets engine-owned identity."""
        payload = evidence.payload or {}
        state = RESTORE_STATE_OF.get(payload.get("state"), State.OFF)
        self.enter_state_silently(
            state, state_since=payload.get("state_since"), hypothesis=hypothesis, cycle_id=payload.get("cycle_id")
        )
        return state

    # detector wiring + tick loop (spec 5.1): one self-re-arming run_in, never run_every("now")
    def wire_detectors(self) -> None:
        for det in self._detectors:
            det.wire(self._ctx)

    def tick(self) -> None:
        for det in self._detectors:
            det.tick(self._ctx)

    def start(self, tick_interval_s: float = 60.0) -> None:
        self.wire_detectors()
        self._arm_tick_loop(tick_interval_s)

    def _arm_tick_loop(self, interval_s: float) -> None:
        self._tick_handle = self._scheduler.run_in(interval_s, lambda: self._tick_loop(interval_s))

    def _tick_loop(self, interval_s: float) -> None:
        self.tick()
        self._arm_tick_loop(interval_s)
