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
import tempfile
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
            published = getattr(engine, "published", None)  # NOTE(integration): confirmed - real
            # ApplianceFSM.published is a plain property; _RealEngineHandle below mirrors it 1:1.
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
            # NOTE(integration): confirmed - real ApplianceFSM has no bare initialize(); this
            # rebuilds engine+policy from the store file (dryer_shadow.py's boot sequence,
            # mirrored by _RealEngineHandle.initialize() below), never a method on the raw FSM.
            engine.initialize()
        elif kind == "force_emptied":
            ev2 = Evidence(EvidenceType.FORCE_EMPTIED, EventClass.PHYSICAL, now, "replay.force_emptied", True, {})
            engine.submit(ev2)
        elif kind == "tick":
            pass  # advance_to() above already did the only thing a bare tick needs to do


# Must equal build_synthetic_tapes.py's REPLAY_FINGERPRINT so corroborate_restore's fingerprint
# check never fires by accident - each store-sourced restore tape then exercises only the
# power/energy-timing reasoning it was designed around, not an incidental mismatch.
_ADAPTER_FINGERPRINT = "replay-fixed-fingerprint"

# Fixed placeholder entity IDs this adapter wires the real policy's power/door/energy config
# knobs to; get_state/get_history below recognize exactly these three strings.
_POWER_SENSOR = "sensor.dryer_plug_power"
_DOOR_SENSOR = "binary_sensor.dryer_door_contact"
_ENERGY_SENSOR = "sensor.dryer_plug_energy"


class _RealEngineHandle:
    """Built ONLY by build_engine_adapter(); wraps a real ApplianceFSM so it satisfies exactly
    the surface Replay.run() touches (submit/initialize/published - see the module docstring's
    integration-seams note). Everything engine-specific lives here, never in Replay itself.

    Two structural gaps this class closes between Replay's simplified event model and the real
    engine's detector-driven one:
      1. submit() does NOT forward Replay's raw POWER_HIGH/POWER_LOW/DOOR_OPENED/DOOR_CLOSED
         straight to fsm.submit() - the real confirm-timer/keep-fresh logic lives entirely
         inside appliance_detectors.py's Detector.on_state callbacks, which only fire off a
         subscribed sensor CHANGE, never off a pre-classified Evidence. So submit() instead
         replays the underlying sensor change through this adapter's own subscribe() registry
         (exactly what a real listen_state firing would do), letting the real detectors decide
         what evidence (if any) to emit. FORCE_EMPTIED is the one type with no detector at all
         (dryer_shadow.py submits it directly too) - forwarded straight to fsm.submit().
      2. get_state/get_history are pure functions of (tape, clock.now()) - no separate mutable
         sensor dict to keep in sync - so history-arbitrated routing (door-edge checks,
         RECONCILE's power-history window) sees exactly the tape's own event series.
    """

    def __init__(self, tape, clock, fake_scheduler):
        # Defensive sibling import, same shape as dryer_monitor.py:45-60 / dryer_shadow.py's own
        # imports - apps/appliances/ is normally on sys.path (AppDaemon) or already inserted by
        # the caller's own test harness; this covers the case where neither has happened yet.
        try:
            from appliance_fsm import ApplianceFSM, RESTORE_STATE_OF, State
        except ImportError:
            import sys

            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from appliance_fsm import ApplianceFSM, RESTORE_STATE_OF, State
        from appliance_detectors import DoorEdgeDetector, PlugOutageDetector, PowerEndDetector, PowerStartDetector
        import dryer_policy as policy_mod
        from cycle_store import CycleStore, parse_utc

        self._ApplianceFSM = ApplianceFSM
        self._RESTORE_STATE_OF = RESTORE_STATE_OF
        self._State = State
        self._detector_classes = (PowerStartDetector, PowerEndDetector, DoorEdgeDetector, PlugOutageDetector)
        self._policy_mod = policy_mod
        self._parse_utc = parse_utc

        self.tape = tape
        self.anchor = _parse_anchor(tape)
        self.clock = clock
        self._scheduler = _FakeSchedulerToRealAdapter(fake_scheduler)
        self._events = sorted(tape.get("events", []), key=lambda e: e["t"])
        self._args = tape.get("args") or {}

        self._tmpdir = tempfile.TemporaryDirectory(prefix="replay_dryer_store_")
        store_path = Path(self._tmpdir.name) / "dryer_replay_state.json"
        self._store = CycleStore(store_path, "dryer_shadow", log=self._log)
        self._code_fingerprint = _ADAPTER_FINGERPRINT
        self._programmes_file = str(Path(__file__).resolve().parents[1] / "dryer_programmes.yaml")

        self.actions = RecordingActionSink()  # spec 7.2 ActionSink protocol - already matches
        self.log_calls = []
        self._subscribers: dict = {}
        self._boot_count = 0
        self._fsm = None
        self._policy = None

        # Deliberately NOT self-booting here: every one of this harness's tapes already opens
        # with its own {"t":0,"kind":"restart"} event (spec 10.2's documented convention for
        # "run initialize() against current entity+store"), which - like a real AppDaemon app -
        # IS the first boot. Eagerly initializing here too would double-boot on every tape (the
        # tape's own t=0 restart landing as boot_count==1, wrongly simulating "entity lost" for
        # what is actually the very first boot). published/submit()/tick() lazily self-boot as a
        # safety net only for a (currently nonexistent) tape that omits the t=0 restart.

    def _ensure_booted(self):
        if self._fsm is None:
            self.initialize()

    # ---- the three touchpoints Replay.run()/_dispatch() use --------------------------------

    @property
    def published(self):
        self._ensure_booted()
        return self._fsm.published

    def submit(self, evidence):
        from appliance_fsm import Evidence as RealEvidence, EvidenceType as RealEvidenceType

        self._ensure_booted()
        if evidence.type is EvidenceType.FORCE_EMPTIED:
            self._fsm.submit(RealEvidence.make(RealEvidenceType.FORCE_EMPTIED, self.clock.now(), evidence.source))
            return
        watts = (evidence.payload or {}).get("watts")
        if evidence.type in (EvidenceType.POWER_HIGH, EvidenceType.POWER_LOW):
            self._fire_state_change(_POWER_SENSOR, watts)
        elif evidence.type is EvidenceType.DOOR_OPENED:
            self._fire_state_change(_DOOR_SENSOR, "on")
        elif evidence.type is EvidenceType.DOOR_CLOSED:
            self._fire_state_change(_DOOR_SENSOR, "off")

    def initialize(self):
        """Rebuild engine+policy from the store file + simulated entity-survival semantics,
        mirroring dryer_shadow.py's initialize() minus the AppDaemon host (spec section 7.1).
        Boot 0 (the tape's own `initial` block) models whatever entity/helper survival that
        tape was authored around; every later boot (real_03's restart storm) simulates a full
        HA restart - entity_state/helper_state lost, only the durable store (this adapter's own
        prior publish()es) can carry Running/Paused across it, exactly the scenario spec F2's
        corroboration machinery exists to arbitrate."""
        policy_mod = self._policy_mod
        State = self._state_or_off = self._State  # local alias, readability only

        cfg = dict(policy_mod.DEFAULTS)
        for key in cfg:
            if key in self._args and key != "selectors":
                cfg[key] = self._args[key]
        if "selectors" in self._args:
            cfg["selectors"] = {**cfg["selectors"], **self._args["selectors"]}
        cfg.update({
            "power_sensor": _POWER_SENSOR, "door_sensor": _DOOR_SENSOR, "energy_sensor": _ENERGY_SENSOR,
            "programme_entity": None, "dryness_entity": None, "skane_plus_entity": None,
            "time_minutes_entity": None, "announce_entity": None, "appliance_label": "dryer",
            "announce_message": self._args.get("announce_message", cfg["announce_message"]),
        })

        profiles = policy_mod.load_programme_profiles(self._programmes_file, log=self._log)
        policy = policy_mod.DryerPolicy(config=cfg, profiles=profiles)

        initial = self.tape.get("initial") or {}
        boot_now = self.clock.now()
        if self._boot_count == 0:
            entity_state = initial.get("entity_state")
            seed_store = initial.get("store")
            if seed_store:
                self._store.save(dict(seed_store))
            entity_last_changed = (seed_store or {}).get("state_since") or self.anchor.isoformat()
            helper_state = initial.get("helper_state")
        else:
            entity_state, entity_last_changed, helper_state = None, None, None  # full HA restart

        try:
            store_data = self._store.load()
        except Exception:
            store_data = None

        snap = policy_mod.resolve_boot_snapshot(
            entity_state=entity_state, entity_attrs={}, entity_last_changed=entity_last_changed,
            store_data=store_data, helper_state=helper_state, now=boot_now, cfg=policy.cfg,
        )

        initial_state, hypothesis, cycle_id, state_since = State.OFF, False, None, None
        if snap is not None:
            initial_state = self._RESTORE_STATE_OF.get(snap["state"], State.OFF)
            policy.restore_from(snap["store_fields"])
            cycle_id = snap["cycle_id"]
            state_since = self._parse_utc(snap["state_since"]) if snap["state_since"] else None
            if initial_state in (State.RUNNING, State.PAUSED) and snap["source"] != "entity":
                hypothesis, why = policy_mod.corroborate_restore(
                    boot_watts=self._live_watts(), last_high_energy_at=policy.last_high_energy_at, now=boot_now,
                    cfg=policy.cfg, code_fingerprint=self._code_fingerprint, stored_fingerprint=snap["code_fingerprint"],
                )
                if hypothesis:
                    self._log(f"Restore: {initial_state.name} from {snap['source']} UNCORROBORATED ({why})")

        self._subscribers = {}
        detectors = [cls() for cls in self._detector_classes] + [policy_mod.KeepFreshDetector()]
        fsm = self._ApplianceFSM(
            policy, self.actions, self.clock, self._scheduler, self._publish, self._log,
            config=policy.cfg, get_state=self._get_state, get_history=self._get_history,
            subscribe=self._subscribe, detectors=detectors,
            cooling_period=policy.cfg["cooling_period"], initial_state=State.OFF,
        )
        self._fsm = fsm
        self._policy = policy

        fsm.enter_state_silently(initial_state, state_since=state_since, hypothesis=hypothesis, cycle_id=cycle_id)
        if policy.last_feedback_cycle_id is not None:
            fsm.mark_fed_back(policy.last_feedback_cycle_id)
        policy.rearm_watchdogs_after_restore(fsm.ctx, initial_state, state_since)
        if hypothesis:
            policy.arm_reconcile(fsm.ctx)
            policy.try_conclude_at_boot(fsm.ctx)

        fsm.wire_detectors()  # do NOT arm the tick loop here - "tick" tape events do that (below)
        self._boot_count += 1

    # ---- sensor-change fan-out (bridges Replay's raw evidence to real Detector.on_state) ----

    def _subscribe(self, entity, handler):
        self._subscribers.setdefault(entity, []).append(handler)

    def _fire_state_change(self, entity, new_value):
        for handler in list(self._subscribers.get(entity, ())):
            handler(entity, None, new_value, self._fsm.ctx)

    def tick(self):
        """Tape 'tick' events drive the engine's own 60s detector-tick loop (KeepFreshDetector
        etc. have none today, but future detectors' tick() must still be reachable) without
        this adapter self-arming a real recurring timer Replay does not know how to cancel."""
        if self._fsm is not None:
            self._fsm.tick()

    # ---- get_state/get_history: pure functions of (tape, clock.now()) ----------------------

    def _offset_now(self):
        return (self.clock.now() - self.anchor).total_seconds()

    def _last_value(self, kind, field, at_t, default):
        value = default
        for ev in self._events:
            if ev["t"] > at_t:
                break
            if ev["kind"] == kind:
                value = ev[field]
        return value

    def _get_state(self, entity, **kwargs):
        at_t = self._offset_now()
        if entity == _POWER_SENSOR:
            return self._last_value("power", "watts", at_t, 0.0)
        if entity == _DOOR_SENSOR:
            return self._last_value("door", "state", at_t, "off")
        if entity == _ENERGY_SENSOR:
            return self._last_value("energy", "kwh", at_t, None)
        return None  # UI selectors (programme/dryness/...) are not wired in this harness

    def _live_watts(self):
        v = self._get_state(_POWER_SENSOR)
        return float(v) if isinstance(v, (int, float)) else 0.0

    def _get_history(self, entity, start_time=None, end_time=None, **kwargs):
        kind_field = {_POWER_SENSOR: "watts", _DOOR_SENSOR: "state", _ENERGY_SENSOR: "kwh"}
        kind = {_POWER_SENSOR: "power", _DOOR_SENSOR: "door", _ENERGY_SENSOR: "energy"}.get(entity)
        if kind is None:
            return [[]]
        field = kind_field[entity]
        lo = (start_time - self.anchor).total_seconds() if start_time else float("-inf")
        hi = (end_time - self.anchor).total_seconds() if end_time else float("inf")
        entries = []
        for ev in self._events:
            if ev["kind"] == kind and lo <= ev["t"] <= hi:
                ts = self.anchor + timedelta(seconds=ev["t"])
                entries.append({"state": str(ev[field]), "last_changed": ts.isoformat()})
        return [entries]

    # ---- publish + durable store (mirrors dryer_shadow.py's _publish/_save_store) ----------

    def _publish(self, published, *, internal, store_only, attrs):
        self._policy.sync_watchdogs_on_publish(self._fsm.ctx, internal)
        payload = {
            "state": self._fsm.published,
            "state_since": self._fsm.state_since.isoformat() if self._fsm.state_since else None,
            "cycle_id": self._fsm.cycle_id, "code_fingerprint": self._code_fingerprint,
            **self._policy.to_store_fields(),
        }
        if self._fsm.published == "Off":
            self._store.clear()
        else:
            self._store.save(payload)

    def _log(self, message, level="INFO"):
        self.log_calls.append((level, message))


class _FakeSchedulerToRealAdapter:
    """Wraps this harness's FakeScheduler (run_in(cb, delay_s, **kwargs), dict-kwarg callback -
    spec 10.1, unchanged) into appliance_fsm.Scheduler's protocol (run_in(delay_s, cb), zero-arg
    callback) - the same shape as dryer_shadow.py's own _SchedulerAdapter, wrapping AppDaemon's
    run_in instead. FakeScheduler itself is untouched; this is purely an integration-time seam."""

    def __init__(self, fake_scheduler):
        self._sched = fake_scheduler

    def run_in(self, delay_s, cb):
        return self._sched.run_in(lambda kwargs: cb(), delay_s)

    def cancel(self, handle):
        self._sched.cancel(handle)


def build_engine_adapter(tape, clock, scheduler):
    """The ONLY function in this module allowed to reference ApplianceFSM's real constructor
    (integration-time; see the module docstring's seam #1). `clock`/`scheduler` must be the
    SAME FakeClock/FakeScheduler instances the caller's Replay(tape) uses, so that
    Replay.run()'s own scheduler.advance_to() calls fire the real detectors' confirm-timers -
    typical use: `r = Replay(tape); engine = build_engine_adapter(tape, r.clock, r.scheduler);
    result = r.run(engine)`. Returns a _RealEngineHandle satisfying submit()/initialize()/
    published/tick() - .actions (a RecordingActionSink) and .log_calls are exposed for
    assertions. tick() is exposed for a caller wanting to drive the engine's own detector-tick
    loop explicitly; a tape's "tick" event only advances the clock (Replay's own job) - it does
    NOT call this, since none of the current detectors need the periodic tick to behave
    correctly under replay (KeepFreshDetector reacts on every sample, not on a timer)."""
    return _RealEngineHandle(tape, clock, scheduler)
