"""
Yale lock health - arbitrates the two HA twins of ONE physical lock and publishes
``sensor.apartment_lock``; actively heals whichever twin goes stale.

The lock is exposed twice: ``lock.yale_bt`` (yalexs_ble, local Bluetooth, push) and
``lock.yale`` (the Yale cloud integration, reached over a Wi-Fi bridge that can itself
go dark - ``binary_sensor.yale_bridge`` pings it, ``switch.extender`` is the Z-Wave plug
that powers it, ``button.yale_wake`` makes the bridge wake the lock and refresh). Either
twin can silently miss a push and stick on a stale state: the cloud twin sat "unlocked"
for 1.5 h on 2026-07-12 while the door was actually locked, and the BLE twin did the
exact same thing in reverse on 2026-07-22 (stuck "unlocked" 1.5 h while the cloud twin
correctly saw the auto-lock). Normal operation: the twins agree within ~2-5 s, and the
lock auto-locks ~3 min after unlock (the cloud twin's ``changed_by`` reads "Auto Lock").

The cloud twin is PUSH-ONLY: its ``last_updated`` does not advance between real events,
so "how old is last_updated" is not a valid staleness signal by itself - the arbitration
below only ever compares the two twins' ``last_changed`` against EACH OTHER (and against
the previously arbitrated state / attribution), never against wall-clock "how long ago".

No hardcoded preference: the 2026-07-12 incident taught "trust BLE" (see the old
entry_truth.py); the 2026-07-22 incident is the mirror image and proves that lesson
wrong. ``arbitrate()`` below picks the side that is actually fresher/attributable this
time, every time - see its docstring for the exact tie-break ladder.

Layering: locks -> LockHealth (this app: arbitrate + heal) -> ``sensor.apartment_lock``
-> EntryTruth (+ door) -> ``binary_sensor.apartment_entry_secure``. EntryTruth no longer
touches the raw lock twins at all (see entry_truth.py); this app owns divergence/
invalidity/bridge-outage detection and healing exclusively.

AppDaemon 4.5.13 bug (not ours - see entry_truth.py / smart_cooling.py's ``_publish``):
every ``set_state()`` HTTP publish drops any attribute whose value is False/0/None before
the request body is built. Booleans that legitimately go False often (``diverged``,
``bridge_online``) are therefore published as the STRINGS "true"/"false" instead of a
real bool, and ``stale_side``/``heal_stage`` are always non-empty strings rather than
None so they never silently vanish from the entity either.

Safety invariant: the bridge's power plug must NEVER be left off. The power-cycle
routine always re-asserts ``switch/turn_on`` a safety-check interval after cycling and
logs ERROR (never just retries silently) if the plug still isn't on by then.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

# ---------------------------------------------------------------------------
# Pure module-level constants + functions - unit-testable without an AppDaemon
# runtime (see tests/test_lock_health.py). None of these are yaml-configurable;
# the yaml args list in lock_health.yaml is the exhaustive set of user-facing knobs.
# ---------------------------------------------------------------------------

VALID_LOCK_STATES = frozenset({"locked", "unlocked", "locking", "unlocking", "jammed"})
TRANSITIONAL_LOCK_STATES = frozenset({"locking", "unlocking"})

# Heal-ladder rung wait times (seconds).
WAKE_WAIT_S = 90
RELOAD_WAIT_S = 120
POWER_CYCLE_WAIT_S = 180
PLUG_OFF_WAIT_S = 12
PLUG_SAFETY_CHECK_S = 60

# Self-induced suppression windows: don't let our OWN action look like fresh evidence
# of the same problem (see the module docstring's "never leave the plug off" sibling
# concern - this is "never mistake our own settling for a new episode").
SELF_INDUCED_RELOAD_WINDOW_S = 120
SELF_INDUCED_POWER_CYCLE_WINDOW_S = 180

# just_recovered / flapping derivation windows.
JUST_RECOVERED_WINDOW_S = 120
FLAPPING_WINDOW_S = 600
FLAPPING_THRESHOLD = 4


def is_valid_lock_state(state):
    return state in VALID_LOCK_STATES


def classify_transition(old, new):
    """One of "real" (valid -> different valid), "recovery" (invalid -> valid),
    "loss" (valid -> invalid), "noop" (anything else, including invalid -> invalid
    and valid -> the SAME valid state)."""
    old_valid = is_valid_lock_state(old)
    new_valid = is_valid_lock_state(new)
    if old_valid and new_valid:
        return "real" if new != old else "noop"
    if new_valid and not old_valid:
        return "recovery"
    if old_valid and not new_valid:
        return "loss"
    return "noop"


def _side_result(state, source, diverged=False, stale_side=None, needs_reconcile=False):
    return {"state": state, "source": source, "diverged": diverged,
            "stale_side": stale_side, "needs_reconcile": needs_reconcile}


def _other(side):
    return "cloud" if side == "ble" else "ble"


def _has_attribution(data):
    who = data.get("changed_by")
    return isinstance(who, str) and who.strip() != ""


def arbitrate(bt, cloud, prev_state, tie_seconds=15):
    """Arbitrate the two lock twins. `bt`/`cloud` are dicts: {state, changed
    (tz-aware datetime|None), changed_by (str|None), valid (bool), just_recovered
    (bool), flapping (bool)}. `prev_state` is the PREVIOUSLY published arbitrated
    state (plain string, e.g. "locked"), used only as a tie-break continuity signal.

    Returns {state, source ("ble"|"cloud"|"both"|"none"), diverged, stale_side
    ("ble"|"cloud"|None), needs_reconcile}. `diverged`/`stale_side` are only ever set
    by rule (e) below - every other outcome (agreement, one-side-invalid, a command
    in flight, or a resolved tie) is normal operation, not a fault to heal.

    Rules, in order:
      a. both invalid -> "unknown", source "none".
      b. exactly one valid -> its state wins outright (nothing to disagree with).
      c. both valid, same state -> that state, source "both".
      d. disagree, exactly one side transitional (locking/unlocking) and the other
         settled (locked/unlocked/jammed) -> the settled side wins, NOT flagged
         diverged (a command is in flight; this is routine, not staleness).
      e. genuine disagreement between two valid states that are both settled (or,
         rarer, both transitional - the same recency logic applies either way, on
         purpose: no side gets a hardcoded pass). A just_recovered side cannot win
         (its fresh last_changed is a restore artifact - HA/AD restart or a BLE
         proxy reconnect - not a lock operation); a flapping side cannot win over a
         non-flapping side (unless BOTH sides are just_recovered/flapping, in which
         case that filter is skipped rather than left with zero candidates). Among
         whatever candidates remain, the newer `changed` wins IF the gap exceeds
         `tie_seconds`; the loser becomes stale_side.
      f. tie (gap <= tie_seconds, e.g. right after an HA restart reset both
         timestamps, or a timestamp is missing so no gap can even be measured):
         (1) exactly one side's state equals prev_state -> that side wins
             (continuity - NOT flagged diverged, this is a tie-break, not a fault);
         (2) else exactly one side has a non-empty changed_by -> that side wins
             (real attributable transition);
         (3) else "unknown", needs_reconcile True (caller presses wake once to
             re-establish ground truth) - NEVER a hardcoded side default here.
    """
    sides = {"ble": bt, "cloud": cloud}
    bt_valid = bool(bt.get("valid"))
    cloud_valid = bool(cloud.get("valid"))

    # (a) both invalid: nothing to arbitrate.
    if not bt_valid and not cloud_valid:
        return _side_result("unknown", "none")

    # (b) exactly one valid: it wins: no disagreement is even possible.
    if bt_valid != cloud_valid:
        winner = "ble" if bt_valid else "cloud"
        return _side_result(sides[winner]["state"], winner)

    # both valid from here on.
    bt_state, cloud_state = bt["state"], cloud["state"]

    # (c) agree.
    if bt_state == cloud_state:
        return _side_result(bt_state, "both")

    # (d) exactly one transitional, the other settled -> settled wins, no flag.
    bt_transitional = bt_state in TRANSITIONAL_LOCK_STATES
    cloud_transitional = cloud_state in TRANSITIONAL_LOCK_STATES
    if bt_transitional != cloud_transitional:
        winner = "cloud" if bt_transitional else "ble"
        return _side_result(sides[winner]["state"], winner)

    # (e) genuine disagreement - resolve by recency, demoting unreliable candidates.
    candidates = {"ble", "cloud"}
    recovered = {side for side in candidates if sides[side].get("just_recovered")}
    if recovered and recovered != candidates:
        candidates -= recovered
    flapping = {side for side in candidates if sides[side].get("flapping")}
    if flapping and flapping != candidates:
        candidates -= flapping

    if len(candidates) == 1:
        winner = next(iter(candidates))
        return _side_result(sides[winner]["state"], winner, diverged=True, stale_side=_other(winner))

    bt_changed, cloud_changed = bt.get("changed"), cloud.get("changed")
    if bt_changed is not None and cloud_changed is not None:
        gap = (bt_changed - cloud_changed).total_seconds()
        if abs(gap) > tie_seconds:
            winner = "ble" if gap > 0 else "cloud"
            return _side_result(sides[winner]["state"], winner, diverged=True, stale_side=_other(winner))

    # (f) tie: gap within tie_seconds, or a timestamp is missing (equally
    # un-orderable) - resolve without ever defaulting to a fixed side.
    matches = [side for side in ("ble", "cloud") if sides[side]["state"] == prev_state]
    if len(matches) == 1:
        winner = matches[0]
        return _side_result(sides[winner]["state"], winner)

    attributed = [side for side in ("ble", "cloud") if _has_attribution(sides[side])]
    if len(attributed) == 1:
        winner = attributed[0]
        return _side_result(sides[winner]["state"], winner)

    return _side_result("unknown", "none", needs_reconcile=True)


def can_act(history, now, cooldown_s, cap_n, cap_window_s):
    """Shared cooldown+cap budget check used by both the reload and power-cycle
    budgets. `history` is a list of past action timestamps (tz-aware datetimes, any
    order) that the CALLER owns and appends to only once an action is actually taken
    - this function never appends `now` itself, since merely checking a budget must
    not consume it.

    Mutates `history` in place, dropping entries older than `cap_window_s`: they can
    no longer count toward any future cap check, and callers persist this list across
    AppDaemon reloads, so pruning here is what keeps it from growing without bound.

    True only if BOTH hold: at least `cooldown_s` have passed since the most recent
    action (if any), AND fewer than `cap_n` actions remain within the trailing
    `cap_window_s`.
    """
    cutoff = now - timedelta(seconds=cap_window_s)
    history[:] = [ts for ts in history if ts >= cutoff]
    if history and (now - max(history)).total_seconds() < cooldown_s:
        return False
    if len(history) >= cap_n:
        return False
    return True


class LockHealth(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.lock_bt = a("lock_bt", "lock.yale_bt")
        self.lock_cloud = a("lock_cloud", "lock.yale")
        self.bridge_ping = a("bridge_ping", "binary_sensor.yale_bridge")
        self.bridge_plug = a("bridge_plug", "switch.extender")
        self.wake_button = a("wake_button", "button.yale_wake")
        self.door = a("door_open_entity", "binary_sensor.apartment_door_open")
        self.publish_entity = a("publish_entity", "sensor.apartment_lock")
        self.notify_target = a("notify_target", "mikkel")

        self.divergence_heal_minutes = float(a("divergence_heal_minutes", 4))
        self.invalid_heal_minutes = float(a("invalid_heal_minutes", 5))
        self.bridge_down_heal_seconds = float(a("bridge_down_heal_seconds", 90))
        self.settle_guard_seconds = float(a("settle_guard_seconds", 45))
        self.startup_grace_seconds = float(a("startup_grace_seconds", 150))
        self.action_cooldown_minutes = float(a("action_cooldown_minutes", 5))
        self.plug_cycle_cooldown_minutes = float(a("plug_cycle_cooldown_minutes", 15))
        self.plug_cycle_max_per_2h = int(a("plug_cycle_max_per_2h", 3))
        self.reload_max_per_2h = int(a("reload_max_per_2h", 4))
        self.autolock_check_minutes = float(a("autolock_check_minutes", 4))
        self.wake_nudge_cooldown_minutes = float(a("wake_nudge_cooldown_minutes", 30))
        self.recovery_debounce_minutes = float(a("recovery_debounce_minutes", 2))

        self._state_file = Path(__file__).with_name("lock_health_state.json")
        # get_app must be resolved in sync init - async context returns a Task.
        self._notifier = self.get_app("MobileNotifier")

        self._eval_lock = asyncio.Lock()
        self._startup_at = self.get_now()

        # Per-side recent (transition-class, ts) history -> just_recovered/flapping.
        # Deliberately NOT persisted (see _load_state): the 120s/600s windows these
        # feed are short, and startup_grace_seconds already covers the immediate
        # post-restart period where losing this would matter.
        self._history = {"ble": [], "cloud": []}

        self._last_state = None
        self._last_source = None
        self._diverged_since = None
        self._current_stale_side = None
        self._currently_diverged = False
        self._invalid_since = {"ble": None, "cloud": None}
        self._bridge_down_since = None
        self._clear_since = {"divergence": None, "ble_invalid": None, "cloud_invalid": None, "bridge_down": None}
        self._notified = {"divergence": False, "ble_invalid": False, "cloud_invalid": False,
                           "bridge_down": False, "flapping_ble": False, "flapping_cloud": False,
                           "plug_unavailable": False}
        self._reload_history = []
        self._plug_cycle_history = []
        self._last_action_at = None
        self._last_reload_at = {"ble": None, "cloud": None}
        self._last_power_cycle_at = None
        self._last_wake_nudge_at = None

        self._heal_stage = "idle"
        self._active_ladder = None
        self._ladder_handle = None
        self._ladder_key = None
        self._ladder_cause = None

        self._load_state()

        for ent in (self.lock_bt, self.lock_cloud):
            self.listen_state(self._on_lock_change, ent)
        self.listen_state(self._on_bridge_change, self.bridge_ping)
        self.listen_state(self._on_door_change, self.door)
        # Separate, filtered listener just for the auto-lock corroborator (open -> closed).
        self.listen_state(self._on_door_closed, self.door, new="off", old="on")
        self.run_every(self._tick, "now+30", 60)
        self.log(f"LockHealth initialized - last_state={self._last_state}/{self._last_source}", level="INFO")

    # ---------- listeners ----------
    def _on_lock_change(self, entity, attribute, old, new, kwargs):
        side = "ble" if entity == self.lock_bt else "cloud"
        self._record_transition(side, old, new, self.get_now())
        self.create_task(self._eval())

    def _on_bridge_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._eval())

    def _on_door_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._eval())

    def _on_door_closed(self, entity, attribute, old, new, kwargs):
        now = self.get_now()
        self.run_in(self._check_autolock_corroborator, self.autolock_check_minutes * 60,
                    door_closed_iso=now.isoformat())

    def _tick(self, kwargs):
        self.create_task(self._eval())

    # ---------- main eval loop ----------
    async def _eval(self):
        async with self._eval_lock:
            await self._eval_locked()

    async def _eval_locked(self):
        try:
            now = await self.get_now()
            bt = await self._side_async("ble", now)
            cloud = await self._side_async("cloud", now)

            result = arbitrate(bt, cloud, self._last_state)
            self._currently_diverged = result["diverged"]
            self._current_stale_side = result["stale_side"]

            await self._publish(bt, cloud, result, now)

            self._track_invalid(bt, cloud, now)
            self._track_divergence(result, now)
            await self._track_bridge(now)

            self._last_state = result["state"]
            self._last_source = result["source"]

            if result["needs_reconcile"]:
                await self._maybe_reconcile_wake(now)

            if self._active_ladder is None:
                await self._maybe_start_ladder(now)

            await self._check_all_clear(now)
            self._save_state()
        except Exception as e:
            self.log(f"lock health eval failed: {e} - skipping this cycle", level="ERROR")

    # ---------- gathering + publishing ----------
    async def _side_async(self, side, now):
        entity = self.lock_bt if side == "ble" else self.lock_cloud
        state = await self.get_state(entity)
        last_changed = await self.get_state(entity, attribute="last_changed")
        changed_by = await self.get_state(entity, attribute="changed_by") if side == "cloud" else None
        return self._to_side_dict(side, now, state, last_changed, changed_by)

    def _side_sync(self, side, now):
        entity = self.lock_bt if side == "ble" else self.lock_cloud
        state = self.get_state(entity)
        last_changed = self.get_state(entity, attribute="last_changed")
        changed_by = self.get_state(entity, attribute="changed_by") if side == "cloud" else None
        return self._to_side_dict(side, now, state, last_changed, changed_by)

    def _to_side_dict(self, side, now, state, last_changed, changed_by):
        return {
            "state": state,
            "changed": self._parse_ts(last_changed),
            "changed_by": changed_by,
            "valid": is_valid_lock_state(state),
            "just_recovered": self._just_recovered(side, now),
            "flapping": self._is_flapping(side, now),
        }

    async def _state(self, entity):
        try:
            return await self.get_state(entity)
        except Exception:
            return None

    async def _publish(self, bt, cloud, result, now):
        attrs = {
            "friendly_name": "Apartment lock",
            "icon": "mdi:lock-check",
            "source": result["source"],
            "bt_state": bt["state"],
            "cloud_state": cloud["state"],
            "bt_changed": bt["changed"].isoformat() if bt["changed"] else None,
            "cloud_changed": cloud["changed"].isoformat() if cloud["changed"] else None,
            # AppDaemon 4.5.13 set_state silently DROPS an attribute whose value is
            # False/0/None (see entry_truth.py / smart_cooling.py's _publish for the
            # full story) - diverged/bridge_online legitimately go False often, so they
            # ride as the STRINGS "true"/"false"; stale_side/heal_stage are always a
            # non-empty string ("" / "idle") rather than None so they never vanish either.
            "diverged": "true" if result["diverged"] else "false",
            "stale_side": result["stale_side"] or "",
            "heal_stage": self._heal_stage,
            "bridge_online": "true" if await self._state(self.bridge_ping) == "on" else "false",
            "computed_at": now.isoformat(timespec="seconds"),
        }
        changed_by = None
        if result["source"] == "ble":
            changed_by = bt.get("changed_by")
        elif result["source"] == "cloud":
            changed_by = cloud.get("changed_by")
        elif result["source"] == "both":
            changed_by = cloud.get("changed_by") or bt.get("changed_by")
        if changed_by:
            attrs["changed_by"] = changed_by
        if self._last_action_at is not None:
            attrs["last_heal_action"] = self._last_action_at.isoformat()
        try:
            await self.set_state(self.publish_entity, state=result["state"], replace=True, attributes=attrs)
        except Exception as e:
            self.log(f"publish failed: {e}", level="WARNING")

    # ---------- continuous-condition tracking ----------
    def _track_invalid(self, bt, cloud, now):
        for side, data in (("ble", bt), ("cloud", cloud)):
            if data["valid"]:
                self._invalid_since[side] = None
            elif self._invalid_since.get(side) is None:
                self._invalid_since[side] = now
            if not data["flapping"]:
                key = f"flapping_{side}"
                if self._notified.get(key):
                    self._notified[key] = False

    def _track_divergence(self, result, now):
        if result["diverged"]:
            if self._diverged_since is None:
                self._diverged_since = now
        else:
            self._diverged_since = None

    async def _track_bridge(self, now):
        state = await self._state(self.bridge_ping)
        if state == "off":
            if self._bridge_down_since is None:
                self._bridge_down_since = now
        else:
            self._bridge_down_since = None
        plug_state = await self._state(self.bridge_plug)
        if plug_state not in (None, "unknown", "unavailable"):
            self._notified["plug_unavailable"] = False

    # ---------- per-side transition history (just_recovered / flapping) ----------
    def _record_transition(self, side, old, new, now):
        cls = classify_transition(old, new)
        if cls == "noop":
            return
        hist = self._history.setdefault(side, [])
        hist.append({"cls": cls, "ts": now.isoformat()})
        del hist[:-20]

    def _just_recovered(self, side, now):
        """True if `side` had a recovery transition within the last
        JUST_RECOVERED_WINDOW_S - its freshly-updated last_changed is a restore
        artifact, not a real lock operation (see arbitrate() rule e)."""
        for item in reversed(self._history.get(side, [])):
            ts = self._parse_ts(item.get("ts"))
            if ts is None:
                continue
            if (now - ts).total_seconds() > JUST_RECOVERED_WINDOW_S:
                break
            if item.get("cls") == "recovery":
                return True
        return False

    def _is_flapping(self, side, now):
        """True if `side` has had >= FLAPPING_THRESHOLD validity transitions
        (recovery or loss) within the last FLAPPING_WINDOW_S."""
        count = 0
        for item in reversed(self._history.get(side, [])):
            ts = self._parse_ts(item.get("ts"))
            if ts is None:
                continue
            if (now - ts).total_seconds() > FLAPPING_WINDOW_S:
                break
            if item.get("cls") in ("recovery", "loss"):
                count += 1
        return count >= FLAPPING_THRESHOLD

    # ---------- suppression guards ----------
    def _guards_ok(self, now, allow_in_startup_grace=False):
        if self.get_state(self.door) == "on":
            return False
        for ent in (self.lock_bt, self.lock_cloud):
            changed = self._parse_ts(self.get_state(ent, attribute="last_changed"))
            if changed is not None and (now - changed).total_seconds() < self.settle_guard_seconds:
                return False
        if not allow_in_startup_grace and (now - self._startup_at).total_seconds() < self.startup_grace_seconds:
            return False
        return True

    def _can_heal_act(self, now):
        if self._last_action_at is None:
            return True
        return (now - self._last_action_at).total_seconds() >= self.action_cooldown_minutes * 60

    def _record_action(self, now):
        self._last_action_at = now

    def _reload_window_active(self, side, now):
        last = self._last_reload_at.get(side)
        return last is not None and (now - last).total_seconds() < SELF_INDUCED_RELOAD_WINDOW_S

    def _power_cycle_window_active(self, now):
        return (self._last_power_cycle_at is not None
                and (now - self._last_power_cycle_at).total_seconds() < SELF_INDUCED_POWER_CYCLE_WINDOW_S)

    # ---------- detection (is issue X due for healing right now) ----------
    def _divergence_due(self, now):
        if not self._currently_diverged or self._diverged_since is None:
            return False
        side = self._current_stale_side
        if side is None or self._notified.get("divergence"):
            return False
        if self._reload_window_active(side, now):
            return False
        elapsed_min = (now - self._diverged_since).total_seconds() / 60.0
        return elapsed_min >= self.divergence_heal_minutes

    def _invalid_due(self, side, now):
        since = self._invalid_since.get(side)
        if since is None:
            return False
        if self._notified.get(f"{side}_invalid"):
            return False
        if self._reload_window_active(side, now):
            return False
        if side == "cloud" and self._power_cycle_window_active(now):
            return False
        elapsed_min = (now - since).total_seconds() / 60.0
        return elapsed_min >= self.invalid_heal_minutes

    def _bridge_down_due(self, now):
        since = self._bridge_down_since
        if since is None or self._notified.get("bridge_down"):
            return False
        if self._power_cycle_window_active(now):
            return False
        elapsed_s = (now - since).total_seconds()
        return elapsed_s >= self.bridge_down_heal_seconds

    async def _maybe_start_ladder(self, now):
        # Priority order per the module spec: divergence, invalid-ble, invalid-cloud,
        # bridge-down. The auto-lock corroborator (detector 4) is its own door-triggered
        # path (_check_autolock_corroborator), not part of this per-tick order.
        if self._divergence_due(now):
            side = self._current_stale_side
            if self._is_flapping(side, now):
                await self._notify_flapping(side)
            else:
                cause = f"Yale {self._label(side)} twin stale {self.divergence_heal_minutes:.0f} min (diverged)"
                await self._start_ladder(side, "divergence", cause)
            return
        if self._invalid_due("ble", now):
            if self._is_flapping("ble", now):
                await self._notify_flapping("ble")
            else:
                await self._start_ladder("ble", "ble_invalid",
                                         f"Yale Bluetooth twin invalid {self.invalid_heal_minutes:.0f} min")
            return
        if self._invalid_due("cloud", now):
            if self._is_flapping("cloud", now):
                await self._notify_flapping("cloud")
            else:
                await self._start_ladder("cloud", "cloud_invalid",
                                         f"Yale cloud twin invalid {self.invalid_heal_minutes:.0f} min")
            return
        if self._bridge_down_due(now):
            await self._start_ladder("bridge", "bridge_down",
                                     f"Yale bridge unreachable {self.bridge_down_heal_seconds:.0f}s")

    @staticmethod
    def _label(side):
        return "Bluetooth" if side == "ble" else "cloud"

    # ---------- heal ladders ----------
    # Single-flight (see _active_ladder): only one ladder is ever mid-run at a time.
    # They all bottleneck through the same wake button / bridge plug and the same
    # global action_cooldown_minutes gate anyway, so this keeps the machinery - and the
    # single published heal_stage - simple without giving up real-world concurrency.
    async def _start_ladder(self, issue, key, cause):
        self._active_ladder = issue
        self._ladder_key = key
        self._ladder_cause = cause
        # _eval runs ON the event loop, where AppDaemon's sync-style API (get_state,
        # call_service, run_in) returns un-run coroutines unless awaited - a wake press
        # issued from here would silently never fire. So the actual rung work is
        # deferred to a run_in callback, which AD executes in a worker thread where
        # the sync API blocks and just works (same reason _maybe_reconcile_wake defers).
        self._ladder_handle = await self.run_in(self._ladder_first_rung, 1, issue=issue)

    def _ladder_first_rung(self, kwargs):
        issue = kwargs["issue"]
        if self._active_ladder != issue:
            return  # superseded/cancelled
        if issue == "bridge" or (issue == "cloud" and self.get_state(self.bridge_ping) == "off"):
            self._power_cycle_rung(issue)
        else:
            self._heal_stage = "wake"
            self._press_wake(issue)

    def _press_wake(self, issue):
        now = self.get_now()
        if not self._guards_ok(now):
            self._abort_ladder(issue, "guard blocked wake press")
            return
        if not self._can_heal_act(now):
            self._abort_ladder(issue, "cooldown blocked wake press")
            return
        self.call_service("button/press", entity_id=self.wake_button)
        self._record_action(now)
        self._report_heal(self._ladder_cause or f"Yale {self._label(issue)} lock issue",
                           "pressed Yale wake button")
        self.log(f"LockHealth[{issue}]: pressed wake button", level="INFO")
        self._save_state()
        self._ladder_handle = self.run_in(self._after_wake, WAKE_WAIT_S, issue=issue)

    def _after_wake(self, kwargs):
        issue = kwargs["issue"]
        if self._active_ladder != issue:
            return  # superseded/cancelled
        if not self._condition_active(issue):
            self._resolve_ladder(issue)
            return
        self._reload_rung(issue, attempt=1)

    def _reload_rung(self, issue, attempt):
        now = self.get_now()
        if issue == "cloud" and self.get_state(self.bridge_ping) == "off":
            self._power_cycle_rung(issue)
            return
        if not self._guards_ok(now):
            self._abort_ladder(issue, "guard blocked reload")
            return
        if not self._can_heal_act(now):
            self._abort_ladder(issue, "cooldown blocked reload")
            return
        if not can_act(self._reload_history, now, cooldown_s=0,
                       cap_n=self.reload_max_per_2h, cap_window_s=7200):
            self.log(f"LockHealth[{issue}]: reload budget exhausted - giving up", level="WARNING")
            self._gave_up(issue)
            return
        entity = self.lock_bt if issue == "ble" else self.lock_cloud
        self.call_service("homeassistant/reload_config_entry", entity_id=entity)
        self._reload_history.append(now)
        self._last_reload_at[issue] = now
        self._record_action(now)
        self._heal_stage = "reload"
        self._report_heal(f"Yale {self._label(issue)} twin still not right after the wake press",
                           f"reloaded the {self._label(issue)} integration entry (attempt {attempt})")
        self.log(f"LockHealth[{issue}]: reload attempt {attempt}", level="INFO")
        self._save_state()
        self._ladder_handle = self.run_in(self._after_reload, RELOAD_WAIT_S, issue=issue, attempt=attempt)

    def _after_reload(self, kwargs):
        issue = kwargs["issue"]
        attempt = kwargs["attempt"]
        if self._active_ladder != issue:
            return
        if not self._condition_active(issue):
            self._resolve_ladder(issue)
            return
        if issue == "ble" and attempt < 2:
            self._reload_rung(issue, attempt=2)
        elif issue == "ble":
            self._gave_up(issue)
        else:  # cloud: one reload attempt, then power-cycle
            self._power_cycle_rung(issue)

    def _power_cycle_rung(self, issue):
        now = self.get_now()
        if not self._guards_ok(now):
            self._abort_ladder(issue, "guard blocked power-cycle")
            return
        if not self._can_heal_act(now):
            self._abort_ladder(issue, "cooldown blocked power-cycle")
            return
        plug_state = self.get_state(self.bridge_plug)
        if plug_state in (None, "unknown", "unavailable"):
            self._notify_plug_unavailable()
            self._gave_up(issue)
            return
        if not can_act(self._plug_cycle_history, now, cooldown_s=self.plug_cycle_cooldown_minutes * 60,
                       cap_n=self.plug_cycle_max_per_2h, cap_window_s=7200):
            self.log(f"LockHealth[{issue}]: plug-cycle budget exhausted - giving up", level="WARNING")
            self._gave_up(issue)
            return
        self._heal_stage = "power_cycle"
        self.call_service("switch/turn_off", entity_id=self.bridge_plug)
        self._plug_cycle_history.append(now)
        self._last_power_cycle_at = now
        self._record_action(now)
        self._report_heal(self._ladder_cause or "Yale bridge issue", "power-cycled the Yale bridge plug")
        self.log(f"LockHealth[{issue}]: power-cycling bridge plug", level="INFO")
        self._save_state()
        # Safety mechanics (turn back on, then re-assert) run independently of the
        # ladder's own progress tracking below - they must fire even if the ladder is
        # aborted/cancelled in the meantime. Never leave the bridge unpowered.
        self.run_in(self._plug_turn_on, PLUG_OFF_WAIT_S)
        self._ladder_handle = self.run_in(self._after_power_cycle, POWER_CYCLE_WAIT_S, issue=issue)

    def _after_power_cycle(self, kwargs):
        issue = kwargs["issue"]
        if self._active_ladder != issue:
            return
        if not self._condition_active(issue):
            self._resolve_ladder(issue)
            return
        self._gave_up(issue)

    def _plug_turn_on(self, kwargs):
        self.call_service("switch/turn_on", entity_id=self.bridge_plug)
        self.run_in(self._plug_safety_check, PLUG_SAFETY_CHECK_S)

    def _plug_safety_check(self, kwargs):
        state = self.get_state(self.bridge_plug)
        if state != "on":
            self.log("LockHealth: bridge plug still not on after power-cycle - forcing on again "
                     "(never leave the bridge unpowered)", level="ERROR")
            self.call_service("switch/turn_on", entity_id=self.bridge_plug)

    def _condition_active(self, issue):
        """Re-verify (never assume success) that `issue` is STILL the real problem."""
        now = self.get_now()
        if issue == "bridge":
            return self.get_state(self.bridge_ping) == "off"
        bt = self._side_sync("ble", now)
        cloud = self._side_sync("cloud", now)
        side_data = bt if issue == "ble" else cloud
        if not side_data["valid"]:
            return True
        result = arbitrate(bt, cloud, self._last_state)
        return bool(result["diverged"] and result["stale_side"] == issue)

    def _abort_ladder(self, issue, reason):
        self.log(f"LockHealth[{issue}]: {reason} - backing off, will re-check next cycle", level="DEBUG")
        self._cancel_ladder_handle()
        self._active_ladder = None
        self._heal_stage = "idle"
        self._ladder_key = None
        self._ladder_cause = None

    def _resolve_ladder(self, issue):
        self.log(f"LockHealth[{issue}]: condition cleared on its own - healing worked, staying silent",
                 level="INFO")
        self._cancel_ladder_handle()
        self._active_ladder = None
        self._heal_stage = "idle"
        self._ladder_key = None
        self._ladder_cause = None
        self._save_state()

    def _cancel_ladder_handle(self):
        if self._ladder_handle is not None and self.timer_running(self._ladder_handle):
            self.cancel_timer(self._ladder_handle)
        self._ladder_handle = None

    def _gave_up(self, issue):
        key = self._ladder_key or issue
        trusted = self._last_state or "unknown"
        if issue == "ble":
            message = (f"Bluetooth side of the Yale lock is stuck and reloads didn't help - if this "
                       f"persists the HA box's Bluetooth adapter may be wedged. Trust '{trusted}'.")
        elif issue == "cloud":
            message = (f"Cloud side of the Yale lock is stuck and reload/power-cycle didn't help. "
                       f"Trust '{trusted}'.")
        else:
            message = "Yale bridge is still unreachable after a power-cycle."
        self._heal_stage = "gave_up"
        self._active_ladder = None
        self._ladder_handle = None
        self._ladder_key = None
        self._ladder_cause = None
        self._notified[key] = True
        self._clear_since[key] = None
        self.log(f"LockHealth[{issue}]: gave up ({key}) - {message}", level="WARNING")
        self.create_task(self._notify(message))
        self._save_state()

    # ---------- one-shot side notifications (flapping / plug unavailable) ----------
    async def _notify_flapping(self, side):
        # async + awaited notify: this runs on the event loop (via _maybe_start_ladder),
        # where create_task - like the rest of the sync-style AD API - returns an un-run
        # coroutine unless awaited. _gave_up/_notify_plug_unavailable keep create_task
        # because they only ever run in worker-thread rung callbacks.
        key = f"flapping_{side}"
        if self._notified.get(key):
            return
        self._notified[key] = True
        self._save_state()
        message = (f"Yale {self._label(side)} twin is flapping (repeated connect/disconnect) - "
                   f"leaving it alone instead of reload-storming it until it settles.")
        self.log(f"LockHealth: {side} flapping - notifying instead of healing", level="WARNING")
        await self._notify(message)

    def _notify_plug_unavailable(self):
        if self._notified.get("plug_unavailable"):
            return
        self._notified["plug_unavailable"] = True
        self._save_state()
        self.log("LockHealth: can't power-cycle - plug unavailable", level="ERROR")
        self.create_task(self._notify("Can't power-cycle the Yale bridge - the plug (switch.extender) "
                                      "is unavailable."))

    # ---------- episode bookkeeping (gave-up notify already happened -> all-clear) ----------
    async def _check_all_clear(self, now):
        changed = False
        for key in ("divergence", "ble_invalid", "cloud_invalid", "bridge_down"):
            if not self._notified.get(key):
                continue
            if self._issue_condition_true(key):
                self._clear_since[key] = None
                continue
            since = self._clear_since.get(key)
            if since is None:
                self._clear_since[key] = now
                continue
            if (now - since).total_seconds() / 60.0 >= self.recovery_debounce_minutes:
                self._notified[key] = False
                self._clear_since[key] = None
                changed = True
                if self._active_ladder is None:
                    self._heal_stage = "idle"
                self.log(f"LockHealth: {key} cleared for {self.recovery_debounce_minutes:.0f} min - "
                         f"all-clear", level="INFO")
                await self._notify(f"Yale lock: {self._clear_message(key)}")
        if changed:
            self._save_state()

    def _issue_condition_true(self, key):
        if key == "divergence":
            return self._currently_diverged
        if key == "ble_invalid":
            return self._invalid_since.get("ble") is not None
        if key == "cloud_invalid":
            return self._invalid_since.get("cloud") is not None
        if key == "bridge_down":
            return self._bridge_down_since is not None
        return False

    @staticmethod
    def _clear_message(key):
        return {
            "divergence": "the two twins agree again.",
            "ble_invalid": "the Bluetooth twin is reporting again.",
            "cloud_invalid": "the cloud twin is reporting again.",
            "bridge_down": "the bridge is reachable again.",
        }.get(key, "back to normal.")

    # ---------- auto-lock corroborator (detector 4) ----------
    def _check_autolock_corroborator(self, kwargs):
        door_closed_at = self._parse_ts(kwargs.get("door_closed_iso"))
        if door_closed_at is None:
            return
        if self._last_state != "unlocked" or self._currently_diverged:
            return
        bt_changed = self._parse_ts(self.get_state(self.lock_bt, attribute="last_changed"))
        cloud_changed = self._parse_ts(self.get_state(self.lock_cloud, attribute="last_changed"))
        newest = max([t for t in (bt_changed, cloud_changed) if t is not None], default=None)
        if newest is not None and newest > door_closed_at:
            return  # something DID update since the door closed - corroborated, no nudge needed
        self._maybe_wake_nudge(self.get_now())

    def _maybe_wake_nudge(self, now):
        if self._active_ladder is not None:
            return
        if (self._last_wake_nudge_at is not None
                and (now - self._last_wake_nudge_at).total_seconds() < self.wake_nudge_cooldown_minutes * 60):
            return
        if not self._can_heal_act(now):
            return
        if not self._guards_ok(now):
            return
        self.call_service("button/press", entity_id=self.wake_button)
        self._last_wake_nudge_at = now
        self._record_action(now)
        self._report_heal("Door closed while the lock still read unlocked with no corroborating update",
                           "pressed Yale wake button (auto-lock corroborator nudge)")
        self.log("LockHealth: auto-lock corroborator nudge - pressed wake", level="INFO")
        self._save_state()

    # ---------- tie/needs_reconcile wake press ----------
    async def _maybe_reconcile_wake(self, now):
        if self._active_ladder is not None:
            return
        if not self._can_heal_act(now):
            return
        # Defer the actual press to a worker-thread callback - see _start_ladder for
        # why nothing may call the sync-style AD API from the event loop unawaited.
        await self.run_in(self._do_reconcile_wake, 1)

    def _do_reconcile_wake(self, kwargs):
        now = self.get_now()
        if self._active_ladder is not None:
            return
        # Re-checked here (not just in the scheduler above): several queued evals may
        # each schedule this callback; the first press records the action and the
        # cooldown then swallows the rest.
        if not self._can_heal_act(now):
            return
        # Startup grace is explicitly exempted here: the tie case is exactly what
        # happens right after an HA restart resets both twins' timestamps together.
        if not self._guards_ok(now, allow_in_startup_grace=True):
            return
        self.call_service("button/press", entity_id=self.wake_button)
        self._record_action(now)
        self._report_heal("Yale twins only agree within the tie window (likely just restarted)",
                           "pressed Yale wake button to re-establish ground truth")
        self.log("LockHealth: tie/needs_reconcile - pressed wake once", level="INFO")
        self._save_state()

    # ---------- house-feed reporting ----------
    def _report_heal(self, cause, effect):
        try:
            self.fire_event("house_events_report", cause=cause, effect=effect,
                            icon="mdi:lock-alert", audience="admin")
        except Exception as e:
            self.log(f"house_events_report failed: {e}", level="DEBUG")

    # ---------- notification (entry_truth pattern: awaited + try/except) ----------
    async def _notify(self, message):
        try:
            await self._notifier.notify(title="Front door", message=message, target=self.notify_target)
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")

    # ---------- timestamps ----------
    @staticmethod
    def _parse_ts(raw):
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _iso(dt):
        return dt.isoformat() if dt is not None else None

    # ---------- persistence (atomic tmp + os.replace, house_events pattern) ----------
    def _save_state(self):
        try:
            data = {
                "last_state": self._last_state,
                "last_source": self._last_source,
                "diverged_since": self._iso(self._diverged_since),
                "invalid_since": {k: self._iso(v) for k, v in self._invalid_since.items()},
                "bridge_down_since": self._iso(self._bridge_down_since),
                "clear_since": {k: self._iso(v) for k, v in self._clear_since.items()},
                "notified": dict(self._notified),
                "reload_history": [self._iso(t) for t in self._reload_history],
                "plug_cycle_history": [self._iso(t) for t in self._plug_cycle_history],
                "last_action_at": self._iso(self._last_action_at),
                "last_reload_at": {k: self._iso(v) for k, v in self._last_reload_at.items()},
                "last_power_cycle_at": self._iso(self._last_power_cycle_at),
                "last_wake_nudge_at": self._iso(self._last_wake_nudge_at),
            }
            tmp = self._state_file.with_name(self._state_file.name + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._state_file)
        except Exception as e:
            self.log(f"state save failed: {e}", level="WARNING")

    def _load_state(self):
        try:
            data = json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return
        except Exception as e:
            self.log(f"state load failed: {e}", level="WARNING")
            return
        self._last_state = data.get("last_state")
        self._last_source = data.get("last_source")
        self._diverged_since = self._parse_ts(data.get("diverged_since"))
        inv = data.get("invalid_since") or {}
        self._invalid_since = {"ble": self._parse_ts(inv.get("ble")), "cloud": self._parse_ts(inv.get("cloud"))}
        self._bridge_down_since = self._parse_ts(data.get("bridge_down_since"))
        clear = data.get("clear_since") or {}
        self._clear_since.update({k: self._parse_ts(v) for k, v in clear.items()})
        notified = data.get("notified") or {}
        self._notified.update({k: bool(v) for k, v in notified.items()})
        self._reload_history = [t for t in (self._parse_ts(x) for x in data.get("reload_history") or []) if t]
        self._plug_cycle_history = [t for t in (self._parse_ts(x) for x in data.get("plug_cycle_history") or []) if t]
        self._last_action_at = self._parse_ts(data.get("last_action_at"))
        reload_at = data.get("last_reload_at") or {}
        self._last_reload_at = {"ble": self._parse_ts(reload_at.get("ble")),
                                "cloud": self._parse_ts(reload_at.get("cloud"))}
        self._last_power_cycle_at = self._parse_ts(data.get("last_power_cycle_at"))
        self._last_wake_nudge_at = self._parse_ts(data.get("last_wake_nudge_at"))
        # heal_stage is derived, not persisted directly: a "gave up" episode survives
        # via the notified latches (an AD reload must not silently drop the published
        # heal_stage back to "idle" while an episode is still unresolved and notified).
        if any(self._notified.get(k) for k in ("divergence", "ble_invalid", "cloud_invalid", "bridge_down")):
            self._heal_stage = "gave_up"
