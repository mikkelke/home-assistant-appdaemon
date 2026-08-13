"""
cycle_persistence.py - Shared plumbing for wiring cycle_store.py's on-disk CycleStore into an
appliance monitor (washer_monitor.py, dryer_monitor.py, dishwasher_monitor.py).

Why this exists: all three monitors independently grew the same persistence layer - build a
disk payload, throttle the write, clear the store on Off, snapshot boot-time state once so
restore functions never re-read an entity a write may have just recreated, reject a stale or
future-dated stored Running/Paused - under two different naming schemes (washer's
_maybe_persist_cycle_state/_cycle_store_payload vs. dryer's and dishwasher's
_save_cycle_state/_build_cycle_store_payload). On 2026-08-12 two people fixed the same restart
bug in parallel and collided, because there was no single place where "persist and restore a
cycle" lived. CyclePersistenceMixin is that place.

Deliberately NOT covered here - stays in each monitor, per-appliance:
    - Boot resolution POLICY (which source wins: live entity vs. store vs. helper vs. history
      vs. Off, and in what order). The dishwasher's precedence differs from the other two by
      design. This module supplies the MECHANISM that policy calls (snapshot capture, the
      staleness check, envelope/payload building), never the decision itself.
    - Any detection, guard, threshold, dry_tail, self-heal, or classification logic.
    - Restoring an in-progress cycle's fields back onto self (_restore_running_state and
      friends) - those read _boot_full_state_snapshot()/_boot_store_snapshot() from here, but
      the restore logic itself is boot policy and stays on each monitor.

A monitor using this mixin supplies, as a subclass of hass.Hass:
    - appliance name + default store filename, via one _init_cycle_store() call early in
      initialize() (see its docstring - the default path must be built by the CALLER with its
      own Path(__file__), never this module's).
    - _cycle_store_field_map(): the non-envelope on-disk fields - {disk_key: zero-arg getter}.
      The envelope (state, state_since, cycle_id, entity_recreated_at) is handled once, here,
      via _cycle_store_envelope() - see its docstring for the small per-appliance knobs
      (_cycle_store_empty_value, _cycle_store_state_since(), _cycle_store_cycle_id()) that
      exist because washer's on-disk shape predates this refactor and differs in those four
      specific respects (empty-string vs. null, and different backing attribute names).
    - _cycle_store_trigger_fields(): extra zero-arg getters (beyond the state string, which is
      always checked) whose change forces an immediate write rather than waiting out
      store_min_write_interval_s - see _save_cycle_state.

This module has no AppDaemon dependency of its own beyond what hass.Hass already provides on
the classes that mix it in (self.log, self.get_state, self.args, ...) - see each method's
docstring for exactly what it expects to find on self.
"""

try:
    from cycle_store import CycleStore, format_utc
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cycle_store import CycleStore, format_utc


class CyclePersistenceMixin:
    """Mix into an appliance monitor (alongside hass.Hass) to get a durable on-disk shadow of
    its cycle state for free. See the module docstring for the full split of what lives here
    versus what stays per-appliance."""

    # ----- Small per-appliance knobs (class attributes; override only where they differ) -----

    # The sentinel written for an unset optional envelope string (state_since,
    # entity_recreated_at). Dryer/dishwasher have always written a JSON null here; washer's
    # on-disk shape predates this refactor and has always written "" instead. Both round-trip
    # identically through cycle_store.parse_utc (empty string and None are both falsy to it),
    # so this is purely a wire-format difference, preserved rather than normalised so no
    # on-disk shape changes for an appliance that never asked for one.
    _cycle_store_empty_value = None

    # Off publishes are, for dryer/dishwasher, always accompanied by self.state already being
    # (or about to be) "Off" - no call site has ever disagreed - so a bare `state == "Off"`
    # is enough to clear. Washer alone can reach _set_state_entity with a PUBLISHED state that
    # disagrees with self.state (_on_confirm_changed republishes a value read back from HA, not
    # self.state - see cycle_store's write-vs-self.state distinction in _save_cycle_state) and
    # must not let a stale/mismatched "Off" destroy a durable clock that is still live.
    _cycle_store_off_requires_live_state = False

    # ----- Setup (call once, early in initialize(), before the boot snapshot capture) -----

    def _init_cycle_store(self, *, appliance, default_path):
        """Create this monitor's CycleStore and the throttle/fingerprint bookkeeping
        _save_cycle_state uses to decide when a publish actually needs a disk write.

        `default_path` must be built by the CALLER (`Path(__file__).with_name(...)` in the
        monitor's own module) - this module's own __file__ would resolve to
        cycle_persistence.py, not the appliance monitor, and every test harness in tests/
        relies on self.args["state_file"] (always set by the caller when present) overriding
        it to keep runtime state inside a tmpdir.
        """
        state_file = self.args.get("state_file") or default_path
        self._cycle_store = CycleStore(state_file, appliance, log=self.log)
        self.store_min_write_interval_s = int(self.args.get("store_min_write_interval_s", 300))
        self._store_last_write_at = None
        self._store_last_fingerprint = None
        self._store_last_written_state = None

    # ----- Boot-time snapshot: read once, before the first write, expose to restore code -----

    def _capture_cycle_store_boot_snapshot(self):
        """Read the state entity's bare state, its full attribute snapshot, and the on-disk
        store - all ONCE, here, before any write this boot. On 2026-07-27 a restore function
        read the entity back AFTER _set_state_entity's first write had already recreated it
        with no attributes, so cycle_start_time read back as None forever while state stayed
        Running with no armed exit. Every restore function must consume the snapshot this
        captures (via _boot_full_state_snapshot()/_boot_store_snapshot() below, or the return
        value here directly) instead of a fresh get_state() call, so that bug class cannot
        recur.

        Stores the full-state and store-payload snapshots on self (for the accessors below,
        and so _drop_cycle_store_boot_snapshot() has something to clear) and also returns
        `(existing, boot_full_state, store_data)` directly, since not every monitor's restore
        functions read the snapshot back off self - some take it as an explicit argument
        instead, which is a boot-policy choice this module does not make for them.
        """
        existing = self.get_state(self.state_entity)
        self._boot_full_state = self.get_state(self.state_entity, attribute="all")
        try:
            store_data = self._cycle_store.load()
        except Exception as e:
            self.log(f"CycleStore load raised unexpectedly: {e} - ignoring store for this boot", level="WARNING")
            store_data = None
        self._boot_store_data = store_data
        return existing, self._boot_full_state, store_data

    def _drop_cycle_store_boot_snapshot(self):
        """Drop the boot-time snapshot once the restore dispatch that consumes it has finished,
        so any LATER call to a restore-flavoured helper (e.g. dishwasher's _handle_force_emptied,
        which can run hours after boot) falls back to a live read instead of replaying this
        boot's now-stale snapshot - see _boot_full_state_snapshot / _boot_store_snapshot."""
        if hasattr(self, "_boot_full_state"):
            del self._boot_full_state
        if hasattr(self, "_boot_store_data"):
            del self._boot_store_data

    def _boot_full_state_snapshot(self):
        """The boot-time full-state snapshot captured once in initialize() (see
        _capture_cycle_store_boot_snapshot), or a live read once that snapshot has been dropped
        (_drop_cycle_store_boot_snapshot) or when called without initialize() having run at all,
        as several tests do directly. Never the live entity while the snapshot exists: by the
        time any restore function runs during initialize(), the entity has already been
        republished once (the first write, see _set_state_entity), and after a HA-restart
        erasure that republish recreates it with no attributes at all - the 2026-07-27 bug this
        snapshot exists to prevent."""
        if hasattr(self, "_boot_full_state"):
            return self._boot_full_state
        return self.get_state(self.state_entity, attribute="all")

    def _boot_store_snapshot(self):
        """The on-disk store payload loaded once in initialize() (see
        _capture_cycle_store_boot_snapshot), or {} once that snapshot has been dropped, or when
        called without initialize() having run."""
        data = self._boot_store_data if hasattr(self, "_boot_store_data") else None
        return data if isinstance(data, dict) else {}

    # ----- Staleness / future-clock rejection of a stored Running/Paused candidate -----

    def _cycle_store_validate_running_candidate(
        self, *, state, start_time, saved_at, now, future_skew_s, max_running_hours, max_downtime_hours,
    ):
        """Shared plausibility check for a stored Running/Paused boot candidate - each
        appliance's own boot-resolution code calls this once it has a state/start_time/saved_at
        to check, and trusts the store only when this returns True.

        future_skew_s / max_running_hours / max_downtime_hours are passed in already resolved
        to the units this method uses (seconds, hours, hours) - each appliance keeps reading its
        OWN self.args key, with its own name, default, and unit, for these (dryer/dishwasher:
        boot_future_start_skew_s, seconds, default 60; washer: store_future_skew_minutes,
        minutes, default 5 - a real difference in tolerance, not just spelling, so left alone
        rather than normalised). Only the comparison itself is shared.

        Returns True for a plausible candidate. Returns False, after exactly one WARNING, for a
        missing/unparsable start_time, one implausibly in the future (box clock behind real time
        when it was saved, before NTP synced - trusting it would compare every duration guard
        against a negative run length and could never pass, a permanently wedged Running/Paused),
        one older than max_running_hours (more likely an abandoned/wedged write than a cycle
        worth resuming), or a saved_at older than max_downtime_hours (the box/AppDaemon was down
        long enough that the frozen clock is riskier to trust than falling through to whatever
        the caller's next-best signal is). Does not itself guard against a raise - every caller
        already runs this from inside its own broader boot-resolution try/except.
        """
        if start_time is None:
            self.log(
                f"CycleStore: stored {state} has no usable start_time - ignoring, falling through",
                level="WARNING",
            )
            return False
        skew_s = (start_time - now).total_seconds()
        if skew_s > future_skew_s:
            self.log(
                f"CycleStore: stored {state} start_time is {skew_s / 60:.0f}min in the future "
                f"(box clock behind real time at save time?) - ignoring, falling through",
                level="WARNING",
            )
            return False
        age_hours = (now - start_time).total_seconds() / 3600
        if age_hours > max_running_hours:
            self.log(
                f"CycleStore: stored {state} start_time is {age_hours:.1f}h old (max "
                f"{max_running_hours:.0f}h) - ignoring, falling through",
                level="WARNING",
            )
            return False
        if saved_at is not None:
            downtime_hours = (now - saved_at).total_seconds() / 3600
            if downtime_hours > max_downtime_hours:
                self.log(
                    f"CycleStore: stored payload was saved {downtime_hours:.1f}h ago (max "
                    f"{max_downtime_hours:.0f}h) - ignoring, falling through",
                    level="WARNING",
                )
                return False
        return True

    # ----- Payload building: common envelope (once, here) + per-appliance field map -----

    def _cycle_store_state_since(self):
        """The value to persist as state_since - overridden only by monitors that keep it under
        a different attribute name (washer: self._store_state_since, its own dedicated field,
        never last_state_change - see _cycle_store_field_map's docstring on any concrete monitor
        for why last_state_change itself is never persisted)."""
        return getattr(self, "state_since", None)

    def _cycle_store_cycle_id(self):
        """The value to persist as cycle_id - overridden only by monitors that keep it under a
        different attribute name (washer: self._cycle_id)."""
        return getattr(self, "cycle_id", None)

    def _cycle_store_before_save(self, state, state_changed, now):
        """Hook run once per _save_cycle_state call, immediately before the payload is built and
        written - no-op by default. Washer overrides this to stamp its own state_since the
        moment the persisted state STRING changes (never on a change to any of the other
        trigger fields), so the very save that records a transition also records the correct
        entry time for it, rather than lagging one save behind."""
        return None

    def _cycle_store_trigger_fields(self):
        """Extra zero-arg getters (beyond the state string, which is always checked) whose
        change forces an immediate write rather than waiting out store_min_write_interval_s -
        see _save_cycle_state. Empty by default; each current monitor overrides this with the
        handful of fields whose staleness would otherwise matter for up to
        store_min_write_interval_s (e.g. a just-restored/just-detected start_time)."""
        return ()

    def _cycle_store_field_map(self):
        """The non-envelope on-disk fields for this appliance, as {disk_key: zero-arg getter}.
        Every concrete monitor must supply this - there is no sensible appliance-agnostic
        default."""
        raise NotImplementedError("subclasses must supply their own on-disk field map")

    def _cycle_store_envelope(self, state):
        """The four fields every monitor's payload starts with. `state` is the state STRING
        actually being persisted (see _save_cycle_state's docstring for why that can differ from
        self.state), never read from self here."""
        empty = self._cycle_store_empty_value
        state_since = self._cycle_store_state_since()
        entity_recreated_at = getattr(self, "_entity_recreated_at", None)
        return {
            "state": state,
            "state_since": format_utc(state_since) if state_since else empty,
            "cycle_id": self._cycle_store_cycle_id(),
            "entity_recreated_at": format_utc(entity_recreated_at) if entity_recreated_at else empty,
        }

    def _build_cycle_store_payload(self, state):
        """Snapshot of everything needed to resume `state` after HA wipes the state entity -
        the envelope above plus this appliance's own _cycle_store_field_map(). Deliberately
        excludes last_state_change (the cooling-period clock: None after every restart today,
        which is what *permits* the first post-restart transition - persisting it would let the
        cooling period swallow that transition, exactly the wedge class this file's
        restart-survival tests exist to catch; state_since is its store-safe twin), timer
        handles, and any raw reading ring buffer - all appliance-specific exclusions actually
        enforced by what each _cycle_store_field_map() chooses to include, not by anything here."""
        payload = self._cycle_store_envelope(state)
        for key, getter in self._cycle_store_field_map().items():
            payload[key] = getter()
        return payload

    # ----- The throttled/forced write, and the publish wrapper that drives it -----

    def _cycle_store_safe_log(self, message):
        """A broken self.log (or a fixture that never finished setting one up) must never turn
        a store problem into a crash - mirrors CycleStore._safe_log's own defensive stance one
        layer up."""
        try:
            self.log(message, level="WARNING")
        except Exception:
            pass

    def _save_cycle_state(self, new_state=None, force=False):
        """Persist `new_state` (defaults to self.state) to the on-disk store so a HA restart -
        which erases the state entity and the cycle clock living in its attributes - cannot
        silently lose an in-progress cycle. Off clears the store outright so a stale payload can
        never seed the next boot.

        new_state is the state actually being PUBLISHED, not necessarily self.state: most
        callers keep the two in lockstep, but a republish of a value read back from HA (e.g.
        washer's _on_confirm_changed) can disagree with self.state (documented AD 4.5.13
        stale-get_state behaviour, or the entity being erased at that exact moment). The store
        must record what HA was actually just told, never a stale self.state - so every write
        here uses `state`, resolved from new_state, throughout.

        Self-throttled: a fingerprint of the state string plus this appliance's own
        _cycle_store_trigger_fields() is compared against the last write, so a change to any of
        them forces a write immediately, while an unchanged fingerprint only writes once
        store_min_write_interval_s has elapsed since the last one (a heartbeat, so the on-disk
        copy is never more than that many seconds stale). force=True bypasses the throttle
        unconditionally - used once, right after a boot's restore dispatch finishes, so the
        on-disk payload is complete by construction rather than by the accident of whether
        something downstream happens to force a fresh write anyway.

        Gated on CycleStore.save() actually returning True: a failed write must not be mistaken
        for an up-to-date one and left unretried for up to store_min_write_interval_s. Never
        raises - CycleStore.save() itself never raises, and this stays defensive on its own
        terms since it runs on every single state publish.
        """
        store = getattr(self, "_cycle_store", None)
        if store is None:
            return
        try:
            state = new_state if new_state is not None else getattr(self, "state", None)
            if state == "Off":
                if self._cycle_store_off_requires_live_state and getattr(self, "state", None) != "Off":
                    self._cycle_store_safe_log(
                        f"_set_state_entity published Off but self.state is {self.state!r} - "
                        f"not clearing the durable store for a live cycle"
                    )
                    return
                store.clear()
                self._store_last_write_at = None
                self._store_last_fingerprint = None
                return
            if state is None:
                return
            fingerprint = (state,) + tuple(getter() for getter in self._cycle_store_trigger_fields())
            now = self._now_utc()
            changed = fingerprint != getattr(self, "_store_last_fingerprint", None)
            last_write = getattr(self, "_store_last_write_at", None)
            if not force and not changed and last_write is not None:
                if (now - last_write).total_seconds() < self.store_min_write_interval_s:
                    return
            state_changed = state != getattr(self, "_store_last_written_state", None)
            self._cycle_store_before_save(state, state_changed, now)
            if store.save(self._build_cycle_store_payload(state)):
                self._store_last_write_at = now
                self._store_last_fingerprint = fingerprint
                self._store_last_written_state = state
        except Exception as e:
            self._cycle_store_safe_log(f"_save_cycle_state failed: {e}")

    def _set_state_entity(self, *, _store_only=False, **kwargs):
        """Publish to the state entity and sync the UI input_select when state changes.

        _store_only is internal-only (never an AppDaemon set_state kwarg - popped here, never
        forwarded): forces a store write, bypassing its throttle unconditionally, and skips the
        HA publish (self.set_state) and UI-select resync entirely. Used solely by initialize()'s
        post-restore save, which needs the on-disk payload guaranteed fresh but is NOT
        announcing a new state - self.state was already published once, moments earlier, by the
        FIRST write this boot. A second identical-state HA publish there would be pure risk with
        no upside: this repo has already seen re-sending the same state strip attributes on some
        HA/AppDaemon setups, and AD 4.5.13's merge semantics for that specific case are
        unconfirmed locally.

        Routing every state-changing save through this one method, store-only or not, matters
        beyond style: several tests stub this ONE method wholesale, without isolating the state
        file, to keep initialize() from touching real on-disk state - a call site that bypassed
        it, even a store-only one, would silently start writing real runtime state during those
        tests too.

        Store write happens BEFORE the HA publish, so a crash between the two can never lose a
        transition the entity itself already announced.
        """
        self._save_cycle_state(new_state=kwargs.get("state"), force=_store_only)
        if _store_only:
            return
        self.set_state(self.state_entity, **kwargs)
        st = kwargs.get("state")
        if st is not None:
            self._sync_ui_select(st)
