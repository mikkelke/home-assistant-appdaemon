"""
dryer_shadow.py - AppDaemon app hosting the engine (appliance_fsm + dryer_policy) in shadow mode
(spec section 7). Thin host: all state-machine logic lives in the engine+policy; this file only
wires AppDaemon I/O to the injected seams, tracks divergence against the live app, and publishes
`sensor.dryer_state_v2`. It never writes `sensor.dryer_state`, the live cycle store, or any
selector/announce entity - own listen_state on the same physical sensors, own store file
(`dryer_shadow_state.json`), own log (`dryer_shadow_log`), zero contamination either direction.

Structural notify-incapability (spec 7.2, two independent barriers):
  1. ShadowActions.announce/push_mobile are no-ops that only append to a local list and log -
     there is no Sonos/Mobile handle anywhere on that class.
  2. This class never calls get_app("SonosNotifier")/get_app("MobileNotifier") and has no
     notify_target - a coding error reaching for a notifier would AttributeError, caught by the
     app-level guard, never reaching HA.
Belt (engine hypothesis-gate, appliance_fsm.py's _user_action) AND suspenders (no wire) - the
shadow cannot announce even if the engine tried.

Defensive sibling imports match dryer_monitor.py:45-60's pattern (AppDaemon puts app dirs on
sys.path; the except covers any context where that has not happened yet).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

try:
    from appliance_fsm import ApplianceFSM, Evidence, EvidenceType, RESTORE_STATE_OF, State, SystemClock
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from appliance_fsm import ApplianceFSM, Evidence, EvidenceType, RESTORE_STATE_OF, State, SystemClock

try:
    from appliance_detectors import DoorEdgeDetector, PlugOutageDetector, PowerEndDetector, PowerStartDetector
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from appliance_detectors import DoorEdgeDetector, PlugOutageDetector, PowerEndDetector, PowerStartDetector

try:
    import dryer_policy as policy_mod
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dryer_policy as policy_mod

try:
    from cycle_store import CycleStore, format_utc, parse_utc
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cycle_store import CycleStore, format_utc, parse_utc

E = EvidenceType
_UNAVAILABLE = (None, "unknown", "unavailable")


class ShadowActions:
    """ActionSink barrier 1 (spec 7.2): every method is an inert recorder. No notifier handle
    exists on this class at all, so it is structurally incapable of reaching Sonos/Mobile even if
    the engine's hypothesis gate (the other barrier) were somehow bypassed."""

    def __init__(self, log):
        self._log = log
        self.announced = []
        self.pushed = []
        self.selected = []
        self.resets = []
        self.feedback = []

    def announce(self, message, **kwargs):
        self.announced.append(message)
        self._log(f"[shadow] would announce: {message}", level="INFO")

    def push_mobile(self, message, **kwargs):
        self.pushed.append(message)
        self._log(f"[shadow] would push: {message}", level="INFO")

    def select_option(self, entity, option, **kwargs):
        self.selected.append((entity, option))

    def reset_selectors(self, **kwargs):
        self.resets.append(kwargs)

    def save_feedback(self, record):
        # In-memory only - the shadow never touches dryer_feedback.json (own-store isolation).
        self.feedback.append(record)


class _SchedulerAdapter:
    """Wraps AppDaemon's run_in/cancel_timer into appliance_fsm.Scheduler's protocol (zero-arg
    callbacks; run_in returns an opaque handle; cancel of a stale/fired handle is a no-op)."""

    def __init__(self, app):
        self._app = app

    def run_in(self, delay_s, cb):
        return self._app.run_in(lambda kwargs: cb(), max(0, delay_s))

    def cancel(self, handle):
        try:
            if handle is not None and self._app.timer_running(handle):
                self._app.cancel_timer(handle)
        except Exception:
            pass


class DryerShadow(hass.Hass):
    """Subclasses hass.Hass directly (NOT CyclePersistenceMixin - spec 7.1 says so explicitly:
    "it composes an ApplianceFSM that owns a CycleStore on its own file"). cycle_store.CycleStore
    is used directly instead; the mixin's boot-snapshot/staleness/field-map machinery is
    reimplemented as the pure functions dryer_policy.resolve_boot_snapshot/corroborate_restore
    consume (see that module's own docstring for why - not a mixin call)."""

    def initialize(self):
        self.power_sensor = self.args["power_sensor"]
        self.energy_sensor = self.args["energy_sensor"]
        self.door_sensor = self.args["door_sensor"]
        self.live_state_entity = self.args["live_state_entity"]  # sensor.dryer_state - READ ONLY
        self.state_entity = self.args["state_entity"]  # sensor.dryer_state_v2 - ours
        self.divergence_debounce_s = float(self.args.get("divergence_debounce_s", 90))

        cfg = dict(policy_mod.DEFAULTS)
        for key in cfg:
            if key in self.args and key != "selectors":
                cfg[key] = self.args[key]
        if "selectors" in self.args:
            cfg["selectors"] = {**cfg["selectors"], **self.args["selectors"]}
        cfg.update({
            "power_sensor": self.power_sensor, "energy_sensor": self.energy_sensor, "door_sensor": self.door_sensor,
            "programme_entity": self.args.get("programme_entity"), "dryness_entity": self.args.get("dryness_entity"),
            "skane_plus_entity": self.args.get("skane_plus_entity"), "time_minutes_entity": self.args.get("time_minutes_entity"),
            "announce_entity": self.args.get("announce_entity"), "announce_message": self.args.get("announce_message", cfg["announce_message"]),
            "appliance_label": "dryer",
        })

        programmes_file = self.args.get("programmes_file") or str(Path(__file__).with_name("dryer_programmes.yaml"))
        profiles = policy_mod.load_programme_profiles(programmes_file, log=self.log)
        policy = policy_mod.DryerPolicy(config=cfg, profiles=profiles)

        state_file = self.args.get("state_file") or Path(__file__).with_name("dryer_shadow_state.json")
        self._store = CycleStore(state_file, "dryer_shadow", log=self.log)
        self._code_fingerprint = self._compute_fingerprint()

        actions = ShadowActions(self.log)
        clock = SystemClock()
        scheduler = _SchedulerAdapter(self)
        detectors = [
            PowerStartDetector(), PowerEndDetector(), DoorEdgeDetector(), PlugOutageDetector(),
            policy_mod.KeepFreshDetector(),
        ]

        self._divergence_count = 0
        self._clean_cycles = 0
        self._divergence_count_at_last_off = 0
        self._last_divergence = None
        self._divergence_pending = None
        self._divergence_active = None
        self._prev_internal = None

        # ----- boot snapshot: read v2's OWN full state ONCE, before any write (spec 11.4 shape) -----
        existing = self.get_state(self.state_entity)
        full = self.get_state(self.state_entity, attribute="all")
        entity_attrs = (full or {}).get("attributes") or {}
        entity_last_changed = (full or {}).get("last_changed") or (full or {}).get("last_updated")
        try:
            store_data = self._store.load()
        except Exception as e:
            self.log(f"CycleStore load raised: {e} - ignoring store this boot", level="WARNING")
            store_data = None

        snap = policy_mod.resolve_boot_snapshot(
            entity_state=existing, entity_attrs=entity_attrs, entity_last_changed=entity_last_changed,
            store_data=store_data, helper_state=None, now=clock.now(), cfg=policy.cfg,
        )

        initial_state, hypothesis, cycle_id, state_since = State.OFF, False, None, None
        if snap is not None:
            initial_state = RESTORE_STATE_OF.get(snap["state"], State.OFF)
            policy.restore_from(snap["store_fields"])
            cycle_id = snap["cycle_id"]
            state_since = parse_utc(snap["state_since"]) if snap["state_since"] else None
            if initial_state in (State.RUNNING, State.PAUSED) and snap["source"] != "entity":
                boot_watts = self._live_watts()
                hypothesis, why = policy_mod.corroborate_restore(
                    boot_watts=boot_watts, last_high_energy_at=policy.last_high_energy_at, now=clock.now(),
                    cfg=policy.cfg, code_fingerprint=self._code_fingerprint, stored_fingerprint=snap["code_fingerprint"],
                )
                if hypothesis:
                    self.log(
                        f"Restore: {initial_state.name} from {snap['source']} is UNCORROBORATED ({why}) - "
                        f"suppressing announcements, reconciling in 60s",
                        level="WARNING",
                    )

        self.fsm = ApplianceFSM(
            policy, actions, clock, scheduler, self._publish, self.log,
            config=policy.cfg, get_state=self.get_state, get_history=self.get_history,
            subscribe=self._subscribe, detectors=detectors,
            cooling_period=policy.cfg["cooling_period"], initial_state=State.OFF,
        )
        self._policy = policy
        self._actions = actions

        self.fsm.enter_state_silently(initial_state, state_since=state_since, hypothesis=hypothesis, cycle_id=cycle_id)
        self._prev_internal = initial_state
        if policy.last_feedback_cycle_id is not None:
            # Pre-arm the engine's exactly-once guard for the cycle_id being restored (spec
            # section 6's mark_fed_back seam) - the in-memory guard alone does not survive a
            # restart; this closes that gap even though the current table never re-triggers
            # _finish() from a restored FINISHED/EMPTIED (defence in depth, not a live bug today).
            self.fsm.mark_fed_back(policy.last_feedback_cycle_id)
        policy.rearm_watchdogs_after_restore(self.fsm.ctx, initial_state, state_since)
        if hypothesis:
            policy.arm_reconcile(self.fsm.ctx)
            policy.try_conclude_at_boot(self.fsm.ctx)

        self.listen_event(self._on_force_emptied, "dryer_force_emptied")
        self.fsm.start(tick_interval_s=60)
        self._arm_divergence_tick(60)

        self.log(f"DryerShadow initialized - state: {self.fsm.state.name} (published {self.fsm.published})", level="INFO")

    # ---- fingerprint (mirrors cycle_persistence.py's _compute_code_fingerprint, own module) ----

    def _compute_fingerprint(self):
        try:
            with open(__file__, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return None

    def _live_watts(self):
        v = self.get_state(self.power_sensor)
        try:
            return float(v) if v not in _UNAVAILABLE else 0.0
        except (ValueError, TypeError):
            return 0.0

    # ---- engine seams ----

    def _subscribe(self, entity, handler):
        self.listen_state(lambda e, a, o, n, kw: handler(e, o, n, self.fsm.ctx), entity)

    def _on_force_emptied(self, event_name, data, kwargs):
        self.fsm.submit(Evidence.make(E.FORCE_EMPTIED, self.fsm.ctx.now(), "dryer_force_emptied_event"))

    def _publish(self, published, *, internal, store_only, attrs):
        """The engine's only write path (spec section 2's store-only rule: same-published-value
        transitions persist the store but never re-publish to HA). Also the single point every
        landed transition passes through, so watchdog cancel-sync and divergence tracking cannot
        be missed by a future table row (see dryer_policy.sync_watchdogs_on_publish)."""
        self._policy.sync_watchdogs_on_publish(self.fsm.ctx, internal)

        if internal == State.OFF and self._prev_internal == State.EMPTIED:
            if self._divergence_count == self._divergence_count_at_last_off:
                self._clean_cycles += 1
            else:
                self._clean_cycles = 0
            self._divergence_count_at_last_off = self._divergence_count
        self._prev_internal = internal

        self._save_store(store_only=store_only)
        if store_only:
            self._check_divergence()
            return

        v2_attrs = {"internal_state": attrs["internal_state"], "cycle_id": attrs.get("cycle_id") or ""}
        if attrs.get("hypothesis"):
            v2_attrs["hypothesis"] = "true"
        if self._code_fingerprint:
            v2_attrs["code_fingerprint"] = self._code_fingerprint
        if self._divergence_count:
            v2_attrs["divergence_count"] = str(self._divergence_count)
        v2_attrs["clean_cycles"] = str(self._clean_cycles)
        if self._last_divergence:
            v2_attrs["last_divergence"] = self._last_divergence["text"]
            v2_attrs["last_divergence_at"] = format_utc(self._last_divergence["at"])
            v2_attrs["last_divergence_kind"] = self._last_divergence["kind"]
        self.set_state(self.state_entity, state=published, attributes=v2_attrs, replace=True)
        self._check_divergence()

    def _save_store(self, *, store_only):
        payload = {
            "state": self.fsm.published, "state_since": format_utc(self.fsm.state_since) if self.fsm.state_since else None,
            "cycle_id": self.fsm.cycle_id, "code_fingerprint": self._code_fingerprint,
            **self._policy.to_store_fields(),
        }
        if self.fsm.published == "Off":
            self._store.clear()
        else:
            self._store.save(payload)

    # ---- divergence detection (spec 7.4) ----

    def _check_divergence(self):
        live = self.get_state(self.live_state_entity)
        if live in _UNAVAILABLE:
            return  # HA restart - both apps re-derive independently, never counted
        v2 = self.fsm.published
        now = self.fsm.ctx.now()
        if v2 == live:
            self._divergence_pending = None
            self._divergence_active = None
            return
        if self._divergence_active is not None and self._divergence_active == (v2, live):
            return  # same ongoing divergence - already counted once, do not re-count
        self._divergence_active = None
        if self._divergence_pending is None or self._divergence_pending[:2] != (v2, live):
            self._divergence_pending = (v2, live, now)
            self.log(f"Divergence candidate (debouncing): v2={v2!r} live={live!r}", level="DEBUG")
            return
        elapsed = (now - self._divergence_pending[2]).total_seconds()
        if elapsed >= self.divergence_debounce_s:
            self._divergence_count += 1
            self._clean_cycles = 0
            self._last_divergence = {"text": f"{v2} vs live {live}", "at": now, "kind": "state_mismatch"}
            self.log(
                f"Divergence #{self._divergence_count}: v2={v2!r} vs live={live!r} "
                f"(persisted >= {self.divergence_debounce_s:.0f}s)",
                level="WARNING",
            )
            self._divergence_active = (v2, live)
            self._divergence_pending = None

    def tick_divergence_check(self):
        """The periodic (60s) comparison leg of spec 7.4 - the engine's own tick() drives
        detectors only, never this. A second, independent self-re-arming run_in loop
        (_arm_divergence_tick, armed once from initialize()) calls this every 60s, deliberately
        separate from the engine's tick loop so a divergence-check bug can never touch detector
        cadence, and vice versa."""
        self._check_divergence()

    def _arm_divergence_tick(self, interval_s):
        self.run_in(lambda kwargs: self._divergence_tick_fire(interval_s), interval_s)

    def _divergence_tick_fire(self, interval_s):
        self.tick_divergence_check()
        self._arm_divergence_tick(interval_s)
