"""tests/replay.py - Replay harness for the shared appliance engine (build spec 10-11):
tape JSON format, FakeClock/FakeScheduler, and the Replay driver.

Built in parallel with appliance_fsm.py/appliance_detectors.py (owned by another agent) -
MUST NOT import either. Evidence/EvidenceType/EventClass below are a LOCAL mirror of spec
3.1/3.2, not an import.

Two integration seams, each marked NOTE(integration)/TODO(integration) at its exact spot:
(1) build_engine_adapter() - the only place allowed to reference ApplianceFSM's real
constructor; raises NotImplementedError today, Replay.run(engine) already works with any
caller-supplied engine. (2) Replay.run()'s three touchpoints on `engine`: submit()/
initialize() (pinned by the spec text) and `published` (this harness's own naming for spec
table 1's "publish-map" surface).

Limitation: "power"/"door" tape events synthesize only raw threshold-crossing evidence
(POWER_HIGH/POWER_LOW, DOOR_OPENED/DOOR_CLOSED) - what a detector's on_state() emits for one
sample. Pattern/timer-derived evidence (KEEP_FRESH, *_CONFIRMED, WD_*, RECONCILE) stays the
engine's own job, driven correctly by FakeScheduler once the real engine is wired in.

Stdlib only. ASCII-only strings.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

# Evidence (spec 3.1/3.2): a local mirror, not an import - see the module docstring.
# EventClass keeps all five spec values for completeness though Replay only constructs
# POWER/PHYSICAL. EvidenceType keeps only the members Replay itself constructs - the other
# twelve (*_CONFIRMED, KEEP_FRESH, PLUG_OUTAGE, WD_*, RECONCILE, BOOT_RESTORE) are
# engine/detector-internal and never originate here.
EventClass = enum.Enum("EventClass", "PHYSICAL POWER WATCHDOG CLOCK RESTORE")
EvidenceType = enum.Enum("EvidenceType", "POWER_HIGH POWER_LOW DOOR_OPENED DOOR_CLOSED FORCE_EMPTIED")


@dataclass(frozen=True)
class Evidence:
    """Field-for-field mirror of appliance_fsm.Evidence (spec 3.1)."""
    type: EvidenceType
    event_class: EventClass
    ts: datetime
    source: str
    live: bool
    payload: dict


class RecordingActionSink:
    """Spec 7.2 ActionSink protocol (announce/push_mobile/select_option/reset_selectors) plus
    save_feedback (spec 6) - recording-only, safe to hand a real engine under replay,
    structurally incapable of reaching Sonos/mobile/HA either way."""
    def __init__(self):
        self.calls = []

    def _record(self, kind, **kw):
        self.calls.append({"kind": kind, **kw})

    def announce(self, message, **kw):
        self._record("announce", message=message, **kw)
    def push_mobile(self, message, **kw):
        self._record("push_mobile", message=message, **kw)
    def select_option(self, entity, option, **kw):
        self._record("select_option", entity=entity, option=option, **kw)
    def reset_selectors(self, **kw):
        self._record("reset_selectors", **kw)
    def save_feedback(self, record, **kw):
        self._record("save_feedback", record=record, **kw)

# FakeClock / FakeScheduler (spec 10.1) - load-bearing determinism.


class FakeClock:
    """Aware-UTC now(), settable/advanceable only by the harness, never by wall-clock time."""
    def __init__(self, start):
        self.set(start)
    def now(self):
        return self._now
    def set(self, when):
        if when.tzinfo is None:
            raise ValueError("FakeClock requires an aware datetime")
        self._now = when.astimezone(timezone.utc)
    def advance(self, seconds):
        self.set(self._now + timedelta(seconds=seconds))


@dataclass
class _Scheduled:
    fire_at: datetime
    seq: int
    handle: int
    cb: Callable
    kwargs: dict


class FakeScheduler:
    """run_in/cancel mirror AppDaemon's signature (callback, delay_s, **kwargs). advance_to
    (target) is the sole clock-moving primitive: fires every due callback in (fire_at,
    arm-order) order, moving the clock to each one's own fire_at first - callbacks that
    schedule new due work are picked up in the same pass, which is what makes watchdogs and
    confirm-timers deterministic under replay."""
    def __init__(self, clock):
        self.clock = clock
        self._pending = []
        self._next_handle = 0
        self._seq = 0

    def run_in(self, cb, delay_s, **kwargs):
        self._next_handle += 1
        self._seq += 1
        fire_at = self.clock.now() + timedelta(seconds=max(delay_s, 0))
        self._pending.append(_Scheduled(fire_at, self._seq, self._next_handle, cb, kwargs))
        return self._next_handle

    def cancel(self, handle):
        before = len(self._pending)
        self._pending = [e for e in self._pending if e.handle != handle]
        return len(self._pending) != before

    def advance_to(self, target):
        if target < self.clock.now():
            raise ValueError(f"cannot move backward: now={self.clock.now()} target={target}")
        while True:
            due = [e for e in self._pending if e.fire_at <= target]
            if not due:
                break
            due.sort(key=lambda e: (e.fire_at, e.seq))
            nxt = due[0]
            self._pending.remove(nxt)
            self.clock.set(nxt.fire_at)
            nxt.cb(dict(nxt.kwargs))
        self.clock.set(target)

# Tape format (spec 10.2): loader + validation.

HA_PUBLISHED_VALUES = {"Off", "Running", "Paused", "Unemptied", "Emptied"}
EVENT_KINDS = {"power", "door", "energy", "restart", "force_emptied", "tick"}
TAPE_SOURCES = {"recorder", "feedback", "synthetic"}


class TapeValidationError(ValueError):
    """validate_tape/load_tape error; message always names the offending event/entry by
    index, kind, and t (or t_approx)."""

def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)

def _require(cond, msg):
    if not cond:
        raise TapeValidationError(msg)


def validate_tape(data, name="<tape>"):
    """Validate `data` against the spec 10.2 tape schema; raises on the first problem."""
    _require(isinstance(data, dict), f"{name}: tape must be a JSON object")
    events, expect = data.get("events"), data.get("expect")
    _require(isinstance(events, list), f"{name}: 'events' must be a list")
    _require(isinstance(expect, list), f"{name}: 'expect' must be a list")
    meta = data.get("meta") or {}
    _require(isinstance(meta, dict), f"{name}: 'meta' must be an object")
    src = meta.get("source")
    _require(src is None or src in TAPE_SOURCES, f"{name}: meta.source {src!r} invalid")
    _require(isinstance(data.get("args") or {}, dict), f"{name}: 'args' must be an object")
    _require(isinstance(data.get("initial") or {}, dict), f"{name}: 'initial' must be an object")

    last = None
    for i, ev in enumerate(events):
        tag = f"{name}: event[{i}]"
        _require(isinstance(ev, dict), f"{tag}: must be an object, got {type(ev).__name__}")
        t, kind = ev.get("t"), ev.get("kind")
        tag = f"{tag} (kind={kind!r}, t={t!r})"
        _require(_is_num(t), f"{tag}: 't' must be a number")
        _require(kind in EVENT_KINDS, f"{tag}: 'kind' not one of {sorted(EVENT_KINDS)}")
        _require(last is None or t >= last, f"{tag}: t must be non-decreasing (previous t={last})")
        last = t
        if kind == "power":
            _require(_is_num(ev.get("watts")), f"{tag}: missing/invalid 'watts'")
        elif kind == "door":
            _require(ev.get("state") in ("on", "off", "unavailable", "unknown"), f"{tag}: invalid 'state'")
        elif kind == "energy":
            _require(_is_num(ev.get("kwh")), f"{tag}: missing/invalid 'kwh'")

    last = None
    for i, exp in enumerate(expect):
        tag = f"{name}: expect[{i}]"
        _require(isinstance(exp, dict), f"{tag}: must be an object, got {type(exp).__name__}")
        ta, pub = exp.get("t_approx"), exp.get("published")
        tag = f"{tag} (t_approx={ta!r})"
        _require(_is_num(ta), f"{tag}: 't_approx' must be a number")
        _require(pub in HA_PUBLISHED_VALUES, f"{tag}: 'published' {pub!r} invalid")
        for flag in ("announced", "pushed", "hypothesis"):
            _require(flag not in exp or isinstance(exp[flag], bool), f"{tag}: '{flag}' must be a bool")
        _require(last is None or ta >= last, f"{tag}: t_approx must be non-decreasing (previous={last})")
        last = ta


def load_tape(path):
    """Load and validate one tape JSON file; returns the parsed dict unchanged."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    validate_tape(data, name=path.name)
    return data

# Replay driver (spec 10.4).

_DEFAULT_ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)

def _parse_anchor(tape):
    """t=0 origin: meta.captured_at when present, else a fixed epoch."""
    captured_at = (tape.get("meta") or {}).get("captured_at")
    if captured_at:
        try:
            dt = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return _DEFAULT_ANCHOR


@dataclass
class ReplayResult:
    trace: list  # [{"t": offset_seconds, "published": "Running"}, ...]
    actions: list  # RecordingActionSink.calls, in call order


class Replay:
    """Drives one tape's events through an already-constructed engine (spec 10.4: caller
    builds the engine, Replay(tape).run(engine) drives it) - see the module docstring's
    integration-seams note for the three touchpoints on `engine` used below."""

    def __init__(self, tape, clock=None, scheduler=None):
        self.tape = tape
        self.anchor = _parse_anchor(tape)
        self.clock = clock or FakeClock(self.anchor)
        self.scheduler = scheduler or FakeScheduler(self.clock)
        initial = tape.get("initial") or {}
        self.entities = {
            "power_w": 0.0,
            "door": "off",
            "energy_kwh": None,
            "entity_state": initial.get("entity_state"),
            "entity_attrs": dict(initial.get("entity_attrs") or {}),
            "helper_state": initial.get("helper_state"),
            "store": initial.get("store"),
        }
        # Hand this to the real engine's constructor as `actions=` so `actions` below fills
        # in - see build_engine_adapter()'s sketch.
        self.actions_sink = RecordingActionSink()

    def offset(self):
        return (self.clock.now() - self.anchor).total_seconds()

    def run(self, engine):
        trace = []
        last_published = None
        for ev in sorted(self.tape["events"], key=lambda e: e["t"]):
            self.scheduler.advance_to(self.anchor + timedelta(seconds=ev["t"]))
            self._dispatch(engine, ev)
            published = getattr(engine, "published", None)  # NOTE(integration): attr name TBD
            if published is not None and published != last_published:
                trace.append({"t": self.offset(), "published": published})
                last_published = published
                self.entities["entity_state"] = published
        return ReplayResult(trace=trace, actions=list(self.actions_sink.calls))

    def _dispatch(self, engine, ev):
        kind = ev["kind"]
        now = self.clock.now()
        args = self.tape.get("args") or {}
        if kind == "power":
            watts = float(ev["watts"])
            self.entities["power_w"] = watts
            start_w, stop_w = args.get("start_w", 8), args.get("stop_w", 5)
            if watts >= start_w:
                etype = EvidenceType.POWER_HIGH
            elif watts <= stop_w:
                etype = EvidenceType.POWER_LOW
            else:
                return  # dead zone between stop_w/start_w: sensor recorded, no evidence
            engine.submit(Evidence(etype, EventClass.POWER, now, "replay.power", True, {"watts": watts}))
        elif kind == "door":
            state = ev["state"]
            self.entities["door"] = state
            if state not in ("on", "off"):
                return  # unavailable/unknown: sensor recorded, no edge evidence (spec F7)
            etype = EvidenceType.DOOR_OPENED if state == "on" else EvidenceType.DOOR_CLOSED
            payload = {"power_w": self.entities["power_w"]}
            engine.submit(Evidence(etype, EventClass.PHYSICAL, now, "replay.door", True, payload))
        elif kind == "energy":
            self.entities["energy_kwh"] = float(ev["kwh"])
        elif kind == "restart":
            engine.initialize()  # NOTE(integration): re-resolve boot state from current entity+store
        elif kind == "force_emptied":
            ev2 = Evidence(EvidenceType.FORCE_EMPTIED, EventClass.PHYSICAL, now, "replay.force_emptied", True, {})
            engine.submit(ev2)
        elif kind == "tick":
            pass  # advance_to() above already did the only thing a bare tick needs to do


def build_engine_adapter(tape, clock, scheduler):
    """TODO(integration): the ONLY function in this module allowed to reference
    ApplianceFSM's real constructor. Everything else here (FakeClock, FakeScheduler,
    Evidence, the tape loader, Replay itself) is final and unaffected when this lands -
    Replay.run(engine) already accepts any object exposing submit()/initialize()/published.

    Sketch (fill in once appliance_fsm.py/dryer_policy.py land; adjust only the marked line
    and the `published`/ctx.get_state wiring noted in the module docstring):
        from appliance_fsm import ApplianceFSM
        sink = RecordingActionSink()
        engine = ApplianceFSM(                                # <-- constructor TBD
            appliance=tape.get("meta", {}).get("appliance", "dryer"), args=tape.get("args", {}),
            clock=clock, scheduler=scheduler, actions=sink,
        )
        return engine, sink
    """
    raise NotImplementedError(
        "TODO(integration): appliance_fsm.ApplianceFSM is not landed yet - construct an "
        "engine exposing submit()/initialize()/published and pass it to Replay.run(engine)."
    )
