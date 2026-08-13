"""
DryerMonitor - Tracks dryer state with power monitoring, programme estimation, and keep-fresh detection.

Appliance: Miele TCB150 WP (Heat Pump Dryer)
- Programme classification from energy + runtime (dryer_programmes.yaml), not power-range.
- 80% duration guard before declaring Unemptied; feedback file and learning from confirmed cycles.
- Fill window: door open with low power before fill_window_minutes = add laundry (Off).
- Finish uld (5 min, 0.02 kWh) is not a real cycle (is_real: false).

States:
    - Off: Dryer is idle, empty, or emptied
    - Running: Drying cycle is in progress
    - Paused: Door opened during Running with power still high (checking laundry)
    - Unemptied: Cycle completed (power dropped), door still closed - waiting for user
    - Emptied: Door opened after cycle complete - user is emptying (door still open)

State Transitions:
    - Power goes high while Off -> Running
    - Power drops while Running (door closed) -> Unemptied (reminder to empty)
    - Keep-fresh pattern detected -> Unemptied (main cycle done)
    - Door opens while Running + power LOW -> Emptied (user is emptying it)
    - Door opens while Running + power HIGH -> Paused (checking laundry)
    - Door closes from Paused + power HIGH -> Running (cycle resumes)
    - Door closes from Paused + power LOW + valid cycle -> Unemptied
    - Door closes from Paused + power LOW + invalid cycle -> Off
    - Door opens while Unemptied -> Emptied (user is emptying)
    - Door closes while Emptied -> Off (emptying complete)
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import os
import time
import uuid
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
import statistics

# Durable on-disk shadow of cycle state - sibling module in this same app directory.
# AppDaemon puts app dirs on sys.path, so a plain import normally resolves; the fallback
# covers any context where that has not happened yet (same defensive shape as the sibling
# imports at the top of washer_monitor.py).
try:
    from cycle_store import CycleStore, format_utc, parse_utc
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cycle_store import CycleStore, format_utc, parse_utc


class DryerMonitor(hass.Hass):
    PROGRAMME_PROFILES = {}

    def _safe_cancel_timer(self, handle):
        """Cancel a timer only if still running (avoids invalid-handle warnings)."""
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
                return True
        except Exception:
            pass
        return False

    _APPLIANCE_UI_STATES = frozenset({"Off", "Running", "Paused", "Unemptied", "Emptied"})

    def _sync_ui_select(self, state_str):
        """Mirror state string to input_select helper (Lovelace survives HA restart)."""
        sel = getattr(self, "ui_state_select", None)
        if not sel or state_str not in self._APPLIANCE_UI_STATES:
            return
        try:
            self.call_service("input_select/select_option", entity_id=sel, option=state_str)
        except Exception as e:
            self.log(f"ui_state_select sync failed ({sel!r} -> {state_str}): {e}", level="DEBUG")

    def _set_state_entity(self, *, _store_only=False, **kwargs):
        """Publish to sensor.*_state and sync input_select.* when state changes.

        _store_only is internal-only (never an AppDaemon set_state kwarg - popped here, never
        forwarded): forces a store write, bypassing its throttle unconditionally, and skips the
        HA publish (self.set_state) and UI-select resync entirely. Used solely by
        initialize()'s post-restore save (see the comment at that call site), which needs the
        on-disk payload guaranteed fresh but is NOT announcing a new state - self.state was
        already published once, moments earlier, by the FIRST write this boot. A second
        identical-state HA publish there would be pure risk with no upside: this repo has
        already seen re-sending the same state strip attributes on some HA/AppDaemon setups,
        and AD 4.5.13's merge semantics for that specific case are unconfirmed locally (FIX 5,
        2026-08-12 review).

        Routing EVERY state-changing save through this one method, store-only or not, rather
        than ever calling _save_cycle_state directly from initialize() matters beyond style:
        several other tests in this file's test suite that exercise initialize() stub this ONE
        method wholesale, without isolating state_file, to keep initialize() from touching real
        on-disk state - a call site that bypassed it, even a store-only one, would silently
        start writing a real dryer_cycle_state.json next to this file during those tests too
        (reproduced, not theorized, while implementing FIX 5)."""
        # Store write BEFORE the HA publish, so a crash between the two can never lose a
        # transition the entity itself already announced - see _save_cycle_state.
        self._save_cycle_state(new_state=kwargs.get("state"), force=_store_only)
        if _store_only:
            return
        self.set_state(self.state_entity, **kwargs)
        st = kwargs.get("state")
        if st is not None:
            self._sync_ui_select(st)

    def _save_cycle_state(self, new_state=None, force=False):
        """Persist current cycle state to the on-disk store (see cycle_store.py) so a HA
        restart - which erases sensor.dryer_state and the cycle clock living in its
        attributes - cannot silently lose an in-progress cycle. Off clears the store outright
        so a stale payload can never seed the next boot.

        Self-throttled: a fingerprint of the fields that matter (state string, start_time,
        notification_sent, detected programme, expected duration) is compared against the
        last write, so ANY change to one of those forces a write immediately, while identical
        repeats (e.g. a periodic progress-attribute republish) only write once per
        store_min_write_interval_s. force=True bypasses the throttle unconditionally - used by
        initialize() right after the restore dispatch, so the on-disk payload is correct by
        construction at the end of every boot (start_time etc. only become non-None partway
        through that dispatch, well after the FIRST write of this boot already ran) rather than
        by the accident of whether something downstream happens to force a fresh write anyway.
        Never raises - CycleStore.save() itself never raises, and this is still wrapped
        defensively since it runs on every single state publish."""
        store = getattr(self, "_cycle_store", None)
        if store is None:
            return
        try:
            state = new_state if new_state is not None else getattr(self, "state", None)
            if state == "Off":
                store.clear()
                self._store_last_write_at = None
                self._store_last_fingerprint = None
                return
            if state is None:
                return
            fingerprint = (
                state,
                self.start_time,
                self.notification_sent,
                self.detected_programme,
                self.expected_dur_at_start,
            )
            now = self._now_utc()
            changed = fingerprint != getattr(self, "_store_last_fingerprint", None)
            last_write = getattr(self, "_store_last_write_at", None)
            if not force and not changed and last_write is not None:
                if (now - last_write).total_seconds() < self.store_min_write_interval_s:
                    return
            payload = self._build_cycle_store_payload(state)
            if store.save(payload):
                self._store_last_write_at = now
                self._store_last_fingerprint = fingerprint
        except Exception as e:
            self.log(f"_save_cycle_state failed: {e}", level="WARNING")

    def _build_cycle_store_payload(self, state):
        """Snapshot of everything needed to resume `state` after HA wipes sensor.dryer_state.
        Deliberately excludes last_state_change (see its init comment - persisting it would let
        the cooling period swallow the very first post-restart transition, exactly the class of
        wedge this file has hit before; state_since is its store-safe twin, see the same
        comment), timer handles, and the power-reading ring buffer (all refill within a minute
        or two of boot)."""
        return {
            "state": state,
            "state_since": format_utc(self.state_since) if self.state_since else None,
            "cycle_id": self.cycle_id,
            "entity_recreated_at": format_utc(self._entity_recreated_at) if self._entity_recreated_at else None,
            "cycle_start_time": format_utc(self.start_time) if self.start_time else None,
            "energy_at_start": self.energy_start,
            "detected_programme": self.detected_programme,
            "programme_duration_min": self.expected_dur_at_start,
            "max_power_w": self.max_power_w,
            "door_opened_during_cycle": self.door_opened_during_cycle,
            "door_opened_time": format_utc(self.door_opened_time) if self.door_opened_time else None,
            "keep_fresh_detected": self.keep_fresh_detected,
            "high_power_counter": self.high_power_counter,
            "notification_sent": self.notification_sent,
        }

    def initialize(self):
        # ----- Configuration -----
        self.power_sensor = self.args["power_sensor"]
        self.energy_sensor = self.args["energy_sensor"]
        self.door_sensor = self.args["door_sensor"]
        self.state_entity = self.args["state_entity"]
        self.ui_state_select = self.args.get("ui_state_entity")
        if self.ui_state_select == "":
            self.ui_state_select = None
        if not self.ui_state_select and self.state_entity and str(self.state_entity).startswith("sensor."):
            self.ui_state_select = "input_select." + str(self.state_entity).split(".", 1)[1]
        self.start_w = float(self.args["start_w"])
        self.stop_w = float(self.args["stop_w"])
        self.run_for = int(self.args["run_for"])
        self.stop_for = int(self.args["stop_for"])
        self.door_close_fast_start_window_s = int(self.args.get("door_close_fast_start_window_s", 600))
        self.door_close_fast_confirm_s = int(self.args.get("door_close_fast_confirm_s", 12))
        # Split selectors: programme, dryness, Skåne+, time (time only for Varm luft). App derives flat key.
        self.programme_entity = self.args.get("programme_entity")
        self.dryness_entity = self.args.get("dryness_entity")
        self.skane_plus_entity = self.args.get("skane_plus_entity")
        self.time_minutes_entity = self.args.get("time_minutes_entity")
        # Reset split selectors when returning to Off so each new cycle starts unconfirmed
        self.reset_programme_on_idle = bool(self.args.get("reset_programme_on_idle", True))
        self.programme_unconfirmed_option = self.args.get(
            "programme_unconfirmed_option", "Auto (unconfirmed)"
        )
        self.dryness_unselected_option = self.args.get("dryness_unselected_option", "—")
        self.reset_time_minutes_on_idle = bool(self.args.get("reset_time_minutes_on_idle", False))
        self.time_minutes_idle_default = self.args.get("time_minutes_idle_default", "20")
        # Announce when finished (match washer): toggle off to silence; app resets to on when Emptied
        self.announce_entity = self.args.get("announce_entity")
        self.announce_message = self.args.get("announce_message", "Dryer is ready to be emptied")

        # Programmes from YAML (replaces programs/power_range for classification)
        self._load_programme_profiles()

        # Feedback and learning
        self.feedback_file = self.args.get("feedback_file") or os.path.join(
            os.path.dirname(__file__), "dryer_feedback.json"
        )
        self._learned_durations = {}
        self._load_and_apply_feedback()

        # Cycle validation and fill window
        self.min_cycle_minutes = int(self.args.get("min_cycle_minutes", 60))
        self.min_energy_kwh = float(self.args.get("min_energy_kwh", 0.05))
        self.fill_window_minutes = int(self.args.get("fill_window_minutes", 60))
        self.pause_timeout_minutes = int(self.args.get("pause_timeout_minutes", 10))

        # History correction: threshold for "high power" in energy series (W)
        self.energy_active_watts = float(self.args.get("energy_active_watts", 100.0))

        # Safety watchdogs - prevent stuck states
        self.max_running_hours = float(self.args.get("max_running_hours", 5))
        self.unemptied_timeout_hours = float(self.args.get("unemptied_timeout_hours", 24))
        # Emptied is the only state with no power- or timer-driven exit of its own: it is left
        # solely by the door closing. Back it with a watchdog (washer/dishwasher both have one)
        # so a missed door-close edge cannot make Emptied terminal - start detection is gated
        # on state == "Off", so a stuck Emptied also blocks the NEXT cycle.
        self.emptied_timeout_minutes = float(self.args.get("emptied_timeout_minutes", 30))

        # State tracking
        self.low_power_timer = None
        self.start_time = None
        self.energy_start = None
        self.poll_timer = None

        # Power pattern detection for keep-fresh
        self.power_readings = []
        self.pattern_check_timer = None
        self.keep_fresh_detected = False
        self.power_reading_interval = 15
        self.pattern_window = 10

        # State stability
        self.last_state_change = None
        # state_since mirrors last_state_change (both stamped together in _should_change_state)
        # but is a SEPARATE attribute so it can be persisted to the on-disk store: restoring
        # last_state_change itself is deliberately never done (see initialize()'s
        # boot-resolution comment - it would let the cooling period swallow the very first
        # post-restart transition), but state_since is exactly the field the store exists to
        # carry for the clock-free states (Unemptied/Emptied watchdogs, Paused's timeout) -
        # see _build_cycle_store_payload.
        self.state_since = None
        self.cooling_period = int(self.args.get("cooling_period", 600))
        self.main_cycle_power = float(self.args.get("main_cycle_power", 400))
        # Overrun-aware ETA: once a cycle runs past its expected duration, the remaining/
        # progress computation floors here instead of pinning at 0/100 (see _overrun_eta).
        self.overrun_floor_min = int(self.args.get("overrun_floor_min", 10))
        self.high_power_counter = 0
        self.high_power_threshold = int(self.args.get("high_power_threshold", 5))
        self.notification_sent = False

        # Pause state tracking
        self.pause_timer = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False

        # Watchdog timers
        self.running_watchdog_timer = None
        self.unemptied_watchdog_timer = None
        self.emptied_watchdog_timer = None

        # Programme classification and guard
        self.expected_dur_at_start = None
        self.detected_programme = "unknown"
        # Snapshot of what _restore_running_state actually restored into detected_programme,
        # taken there before that same method's own _update_running_attributes() call
        # reclassifies and overwrites it - see the boot self-heal below and that snapshot's own
        # comment. None until a Running/Paused restore actually runs this boot.
        self._restored_detected_programme = None
        self.max_power_w = 0.0
        self.classify_timer = None
        self.door_fast_start_armed_until = None
        self.start_confirm_timer = None

        # Cycle identity / observability - part of the on-disk store's "common envelope" (see
        # _build_cycle_store_payload). cycle_id is minted fresh only when a NEW Running cycle
        # begins (_confirm_running); restoring a cycle always carries forward whatever value the
        # store/entity already had, never mints a new one. Originally diagnostic only (nothing
        # read it back to gate a decision); as of the 2026-08-12 finish_uld duplicate-feedback
        # fix it is ALSO the key for _save_cycle_feedback's per-cycle duplicate guard just below -
        # a human correlating log lines / feedback records / the on-disk store across a restart
        # can still tell "same physical cycle" from "a new one started right after" from it too.
        self.cycle_id = None
        # Guard for _save_cycle_feedback: the cycle_id (above) that most recently had a feedback
        # record written for it, so a second write for the SAME cycle is refused even if some
        # call site forgets to gate on _transition_to_unemptied's return value (defence in depth
        # - the primary fix is that gate; see _transition_to_unemptied's docstring for the bug
        # this backstops). Reset alongside cycle_id itself, in _confirm_running, when a genuinely
        # new cycle begins - deliberately never touched by _reset_cycle_tracking, matching
        # cycle_id's own lifecycle (both carry forward, unreset, across a transition to Off).
        self._feedback_saved_cycle_id = None
        self._entity_recreated_at = None

        # Durable on-disk shadow of this cycle's state - survives what sensor.dryer_state
        # cannot: HA erases AppDaemon set_state entities, and the cycle clock living in their
        # attributes, on every restart (see cycle_store.py's module docstring). Filename MUST
        # end in _state.json: .gitignore excludes that pattern and deploy.sh only rsyncs
        # git-tracked files, so this runtime file can never accidentally ship or get committed.
        state_file = self.args.get("state_file") or Path(__file__).with_name("dryer_cycle_state.json")
        self._cycle_store = CycleStore(state_file, "dryer", log=self.log)
        self.store_min_write_interval_s = int(self.args.get("store_min_write_interval_s", 300))
        # Boot-restore staleness: a stored Running/Paused whose clock is this old, or whose
        # payload was saved this long ago, is more likely an abandoned/wedged write than a
        # cycle worth resuming - reject it and fall through to Off. Clock-free restored states
        # (Unemptied/Emptied) get no equivalent extra check: _restore_remaining_seconds already
        # floors an expired watchdog at 60s, so an over-age one just self-clears within a
        # minute while still leaving a logbook entry.
        self.store_max_downtime_hours = float(self.args.get("store_max_downtime_hours", 12))
        # A stored start_time in the future (box clock behind real time at boot, before NTP
        # syncs) must be rejected outright, not just left to fail the "too old" check above -
        # every duration guard downstream would compare against a negative run length and
        # could never pass, wedging a restored Running/Paused permanently. The small allowance
        # only tolerates second-level rounding in the stored timestamp, not genuine clock skew.
        self._boot_future_start_skew_s = float(self.args.get("boot_future_start_skew_s", 60))
        self._store_last_write_at = None
        self._store_last_fingerprint = None

        # ----- Boot state resolution: entity -> store -> helper -> Off -----
        # A HA restart erases sensor.dryer_state (and the cycle clock in its attributes)
        # outright; an AppDaemon-only restart leaves the entity untouched. Read the entity's
        # full state ONCE, here, before any write - _set_state_entity below is the first write
        # this boot, and on 2026-07-27 a restore function read the entity back AFTER that write
        # had already recreated it with no attributes, so its own cycle_start_time read back as
        # None forever while state stayed Running with no armed exit. Every restore function
        # now consumes this same captured snapshot (self._boot_full_state) instead of a live
        # read, so that bug class cannot recur.
        existing = self.get_state(self.state_entity)
        self._boot_full_state = self.get_state(self.state_entity, attribute="all")
        try:
            store_data = self._cycle_store.load()
        except Exception as e:
            self.log(f"CycleStore load raised unexpectedly: {e} - ignoring store for this boot", level="WARNING")
            store_data = None
        self._boot_store_data = store_data

        valid_states = ("Running", "Unemptied", "Paused", "Emptied")
        entity_sourced = existing in valid_states
        if not entity_sourced:
            # Entity missing/unknown/unavailable - HA likely erased it. Record when, purely
            # for observability in the store payload; carry the original stamp forward across
            # an AD-only restart within the same erased-entity episode when the store has one
            # (e.g. AppDaemon itself crash-looping before ever completing its first
            # post-erasure republish a few lines below) - only stamp a fresh "now" the first
            # time this episode is observed, matching this comment (previously this stamped
            # "now" unconditionally here, contradicting it - see FIX 6, 2026-08-12 review).
            prior = parse_utc(store_data.get("entity_recreated_at")) if store_data else None
            self._entity_recreated_at = prior or self._now_utc()
        elif store_data:
            self._entity_recreated_at = parse_utc(store_data.get("entity_recreated_at"))

        # state_since (see its init comment): prefer the entity's own last_changed from the
        # boot snapshot - meaningful only when the entity is itself live (AD-only reload; after
        # a HA-restart erasure it would just be this boot's own recreation instant, worthless
        # exactly when the store matters most) - else carry forward whatever the store's own
        # state_since already was, rather than treating "now" as a fresh one.
        boot_last_changed = (
            (self._boot_full_state.get("last_changed") or self._boot_full_state.get("last_updated"))
            if entity_sourced and isinstance(self._boot_full_state, dict) else None
        )
        if boot_last_changed:
            self.state_since = parse_utc(boot_last_changed)
        elif store_data:
            self.state_since = parse_utc(store_data.get("state_since"))

        helper_state = self.get_state(self.ui_state_select) if self.ui_state_select else None

        try:
            if entity_sourced:
                # AD-only reload: the live entity is the most-trusted signal there is - trust
                # it outright, exactly as before this change. Store/helper are not consulted.
                resolved_state = existing
                gate_start_time = None  # not checked below - see the gate's own comment
            else:
                store_state = store_data.get("state") if store_data else None
                store_ok = store_state in valid_states
                store_start = None
                if store_ok and store_state in ("Running", "Paused"):
                    now = self._now_utc()
                    store_start = parse_utc(store_data.get("cycle_start_time"))
                    saved_at = parse_utc(store_data.get("saved_at"))
                    if store_start is None:
                        store_ok = False
                    elif (store_start - now).total_seconds() > self._boot_future_start_skew_s:
                        # A start_time in the future must never be trusted (see the
                        # _boot_future_start_skew_s comment above) - reject exactly like a
                        # too-old one, not silently accepted by the "too old" check below
                        # (which a future timestamp trivially passes, being negative).
                        self.log(
                            f"CycleStore: stored {store_state} start_time is "
                            f"{(store_start - now).total_seconds():.0f}s in the future - "
                            f"rejecting, falling through",
                            level="WARNING",
                        )
                        store_ok = False
                    elif (now - store_start).total_seconds() > self.max_running_hours * 3600:
                        self.log(
                            f"CycleStore: stored {store_state} start_time is "
                            f"{(now - store_start).total_seconds() / 3600:.1f}h old (max "
                            f"{self.max_running_hours}h) - rejecting, falling through",
                            level="WARNING",
                        )
                        store_ok = False
                    elif saved_at and (now - saved_at).total_seconds() > self.store_max_downtime_hours * 3600:
                        self.log(
                            f"CycleStore: stored payload was saved "
                            f"{(now - saved_at).total_seconds() / 3600:.1f}h ago (max "
                            f"{self.store_max_downtime_hours}h) - rejecting, falling through",
                            level="WARNING",
                        )
                        store_ok = False
                if store_ok:
                    resolved_state = store_state
                    gate_start_time = store_start
                    if helper_state in valid_states and helper_state != store_state:
                        # Write order is store -> entity -> helper, so the store can never be
                        # behind the mirror - it always wins, but a disagreement is still worth
                        # a WARNING (e.g. a stuck/manually-edited helper).
                        self.log(
                            f"CycleStore says {store_state!r} but {self.ui_state_select} says "
                            f"{helper_state!r} - trusting the store (write order is store -> "
                            f"entity -> helper, so the store can never be behind)",
                            level="WARNING",
                        )
                elif helper_state in ("Running", "Paused"):
                    # The mirror never carries a clock - it is a bare state string, always was.
                    # The store above already proved unable to supply one either (missing,
                    # wrong appliance, corrupt, or its own start_time/age was just rejected),
                    # so seeding the name from the helper here would only be caught by the gate
                    # below anyway; log the more specific, informative reason instead.
                    self.log(
                        f"{self.ui_state_select} says {helper_state} but no trustworthy cycle "
                        f"clock is available from the entity or the on-disk store - not "
                        f"seeding; power detection re-establishes a dryer that is still running",
                        level="INFO",
                    )
                    resolved_state = "Off"
                    gate_start_time = None
                elif helper_state in valid_states:
                    resolved_state = helper_state
                    gate_start_time = None
                    self.log(
                        f"State seeded from {self.ui_state_select} - sensor was missing (HA restart?)",
                        level="INFO",
                    )
                else:
                    resolved_state = "Off"
                    gate_start_time = None

            # Gate: self.state is only ever assigned Running/Paused in a branch that already
            # holds a non-None start_time. Checked here for store/helper-sourced resolutions;
            # entity-sourced ones are trusted outright (see above) and in practice always do
            # carry one, since the same code path always writes both together. If no
            # trustworthy start_time is recoverable from any source, fall through to Off - a
            # dryer that never ran looks the same. (Historical note: before the on-disk store
            # existed, Running/Paused could never be seeded at all after an erase, because the
            # ONLY carrier of a cycle clock was the entity itself - precisely what HA erases.
            # The store now fixes that for the store-sourced path; the helper alone still never
            # carries a clock, so a helper-only resolution can never populate a start_time and
            # is pre-empted above before it would even reach this gate.)
            if not entity_sourced and resolved_state in ("Running", "Paused") and gate_start_time is None:
                self.log(
                    f"Boot resolve: {resolved_state} has no trustworthy start_time from entity "
                    f"or store - falling through to Off",
                    level="WARNING",
                )
                resolved_state = "Off"
        except Exception as e:
            # A corrupt store, or any other unexpected failure in the resolution above,
            # degrades to the pre-store behaviour rather than taking the app down with it.
            self.log(f"Boot state resolution failed: {e} - falling back to legacy resolution", level="WARNING")
            resolved_state = existing if existing in valid_states else "Off"
            if resolved_state == "Off" and helper_state in ("Unemptied", "Emptied"):
                resolved_state = helper_state
                self.log(
                    f"State seeded from {self.ui_state_select} - sensor was missing (HA restart?)",
                    level="INFO",
                )

        self.state = resolved_state
        # A helper-seeded Unemptied/Emptied (see the helper_state branches above) carries no
        # clock at all: entity_sourced is False (nothing to read last_changed from) and the
        # store had nothing usable either (else this boot would have store-sourced instead), so
        # state_since - set above only from boot_last_changed or store_data - is still None
        # here. Left unfixed, that None persists to the store (_build_cycle_store_payload) and
        # every subsequent HA erasure re-arms the Unemptied/Emptied watchdog re-armed in
        # _restore_unemptied_state / _restore_emptied_state below from its full period instead
        # of any remaining time (FIX 4, 2026-08-12 review: with six HA restarts in one day, as
        # happened today, the auto-clear then never fires at all). Stamp "now" so the clock at
        # least starts somewhere, same as a state genuinely entered for the first time right now
        # would get via _should_change_state.
        if self.state != "Off" and self.state_since is None:
            self.state_since = self._now_utc()
        self._set_state_entity( state=self.state)

        # If we restored Running/Paused, restore start_time and timers so we keep polling
        # and can detect power drop (fixes stuck "Running" after AppDaemon restart).
        if self.state in ("Running", "Paused"):
            self._restore_running_state()
        elif self.state == "Unemptied":
            # Re-arm the 24h auto-clear watchdog (see _restore_unemptied_state) - unlike the
            # door-open listener this is not persisted and would otherwise stay disarmed
            # across every deploy.
            self._restore_unemptied_state()
        elif self.state == "Emptied":
            # Same reasoning as Unemptied, and more urgent: Emptied's only exit is the door
            # closing, so a deploy landing while Emptied would leave it with no backstop at all.
            self._restore_emptied_state()

        # The FIRST write above (_set_state_entity a few lines up) necessarily persisted a
        # store payload from BEFORE the restore dispatch populated start_time/energy_start/etc,
        # since those only exist inside the _restore_* calls just above. For Running/Paused
        # that gets silently corrected as a side effect of _restore_running_state calling
        # _update_running_attributes (a second, self-triggered write) - but Unemptied/Emptied
        # have no such second write at all. _store_only=True (FIX 5, 2026-08-12 review) so this
        # forces the store write without a second, redundant HA publish of the exact same state
        # _set_state_entity a few lines up already sent: that used to republish to HA a second
        # time with no attributes right after _restore_running_state's own
        # _update_running_attributes call (Running/Paused) or the Unemptied/Emptied restore
        # helpers just wrote the real ones - a second identical-state publish this repo has
        # already seen strip attributes on some HA/AppDaemon setups (see _set_state_entity's own
        # docstring), and AD 4.5.13's merge semantics for that specific case are unconfirmed
        # locally. Still forces the store write unconditionally so this always lands rather
        # than being skipped by the throttle when nothing in the fingerprint happens to have
        # changed (e.g. helper-seeded Unemptied, where start_time/notification_sent never move).
        self._set_state_entity(state=self.state, _store_only=True)

        # Boot-only snapshot: drop it now so any future call to a restore-flavoured helper
        # falls back to a live read instead of replaying this boot's stale snapshot - see
        # _boot_full_state_snapshot. None of this file's restore methods are currently called
        # again after initialize(), but this keeps that guarantee mechanical rather than
        # incidental (dishwasher_monitor.py's _handle_force_emptied does call its equivalent
        # again at runtime, which is exactly the scenario this guards against).
        if hasattr(self, "_boot_full_state"):
            del self._boot_full_state
        if hasattr(self, "_boot_store_data"):
            del self._boot_store_data

        # Listen for events
        self.listen_state(self._power_changed, self.power_sensor)
        self.listen_state(self._door_state_changed, self.door_sensor)
        self.listen_state(self._handle_unavailable, self.state_entity, new="unavailable")
        self.listen_state(self._handle_unavailable, self.power_sensor, new="unavailable")
        # Dashboard "Emptied" button (2026-08-07) - same convention as dishwasher_force_emptied.
        self.listen_event(self._handle_force_emptied, "dryer_force_emptied")

        # Get Sonos Notifier App instance
        self.sonos_notifier = None
        try:
            self.sonos_notifier = self.get_app("SonosNotifier")
            if self.sonos_notifier:
                self.log("Successfully got instance of SonosNotifier.", level="INFO")
            else:
                self.log("WARN: SonosNotifier app not found.", level="WARNING")
        except Exception as e:
            self.log(f"WARN: Error getting SonosNotifier app: {e}", level="WARNING")

        # Unavailable-power grace: tolerate a short power-sensor dropout (HA restart, ESPHome
        # OTA flash - these routinely blip the plug for well under this) before wiping an
        # in-progress cycle; see _handle_unavailable and washer_monitor.py's
        # power_unavailable_off_after_seconds (same guard, proven there since 2026-07-17 - an
        # instant force-Off used to destroy cycle tracking/learning data on every plug blip).
        self.power_unavailable_off_after_seconds = int(self.args.get("power_unavailable_off_after_seconds", 180))
        self.power_unavailable_off_timer = None

        # Dead-plug watchdog: unlike the dishwasher there is no Error state here - a plug that
        # stays unavailable past the grace above still forces Off, so a dead Shelly is
        # indistinguishable from an idle dryer. After this grace, page the phone; one
        # push per outage + all-clear on recovery (gw2000a_watchdog policy: dead sensor =
        # maintenance to act on, not house-feed material).
        self.plug_outage_push_after_seconds = int(self.args.get("power_unavailable_push_after_seconds", 180))
        self.notify_target = self.args.get("notify_target", ["mikkel"])
        self._plug_outage_push_timer = None
        self._plug_outage_pushed = False

        # Bootstrap
        current_power = self.get_state(self.power_sensor)
        if current_power not in ["unknown", "unavailable"]:
            try:
                current_watts = float(current_power or 0)
                self._power_changed(self.power_sensor, None, None, current_watts, {})
            except (ValueError, TypeError):
                self._handle_unavailable(self.power_sensor, None, None, current_power, {})
        else:
            # Plug already dead at app start: the unavailable listener will never fire
            # (no transition), so arm the dead-plug watchdog here (washer does the
            # same in its bootstrap).
            self._begin_plug_outage_grace()

        # Self-heal: a cycle restored as Running may actually have finished while AppDaemon/HA
        # was down - power is already low and we are past the 80% guard. The 5h running
        # watchdog only ever forces Off (see _running_watchdog_timeout), so without this a
        # dryer that finished during downtime would silently lose its empty-me reminder
        # entirely. Ported from dishwasher_monitor.py's equivalent boot self-heal, which has
        # carried this shape since before this file had one.
        #
        # FIX (2026-08-12 review, second pass): anchor to what _restore_running_state actually
        # restored - self._restored_detected_programme (snapshotted there, before that method's
        # own _update_running_attributes() call reclassifies and overwrites
        # self.detected_programme - see that snapshot's own comment) and
        # self.expected_dur_at_start (restored by the same method, never touched again before
        # here) - rather than re-classifying from live elapsed+energy here.
        # _classify_programme() is a pure function of those two numbers and can land on a
        # SHORTER profile than the one actually restored (e.g. a restored long programme sitting
        # at an elapsed/energy point that reclassifies to a shorter one), collapsing guard_dur
        # and forcing a false Unemptied while the dryer is still genuinely running - a dryer has
        # a long low-power cool-down / anti-crease tail where watts <= stop_w on its own is NOT
        # proof the cycle ended. Only the restored clock and programme can tell the difference.
        # This self-heal is brand new for the dryer (no equivalent existed before), so any
        # false-finish path it introduces is a pure regression, not a pre-existing risk.
        if self.state == "Running" and self.start_time:
            current_power_state = self.get_state(self.power_sensor)
            if current_power_state not in ("unknown", "unavailable", None):
                try:
                    watts = float(current_power_state or 0)
                    if watts <= self.stop_w:
                        run_min = self._get_run_duration_minutes()
                        restored_prog = self._restored_detected_programme
                        if restored_prog and restored_prog != "unknown":
                            prog = restored_prog
                        else:
                            prog = self._classify_programme()
                        guard_dur = (
                            self.expected_dur_at_start
                            if self.expected_dur_at_start is not None
                            else self._get_guard_duration(tick_prog=prog)
                        )
                        if run_min >= guard_dur * 0.8 and self._is_valid_completed_cycle():
                            self.log(
                                f"Self-heal: restored Running with power {watts:.1f}W <= "
                                f"{self.stop_w}W and run {run_min:.0f}min past the 80% guard - "
                                f"forcing Unemptied",
                                level="INFO",
                            )
                            run_minutes_wall = run_min
                            run_minutes, duration_source = self._correct_duration(
                                run_minutes_wall, log_prefix="Boot self-heal "
                            )
                            idle_min = (
                                run_minutes_wall - run_minutes
                                if duration_source and run_minutes_wall > run_minutes else None
                            )
                            energy_kwh = self._get_energy_used()
                            became_unemptied = self._transition_to_unemptied(
                                skip_announce=False, run_minutes=run_minutes, energy_used=energy_kwh
                            )
                            if became_unemptied:
                                confirmed, is_human = self._get_confirmed_from_selector(prog)
                                self._save_cycle_feedback(
                                    predicted=prog,
                                    confirmed=confirmed,
                                    duration_min=run_minutes,
                                    energy_kwh=energy_kwh,
                                    max_power_w=self.max_power_w,
                                    programme_confirmed_by_human=is_human,
                                    duration_source=duration_source,
                                    end_reason="boot_self_heal",
                                    idle_min=idle_min,
                                )
                except Exception as e:
                    self.log(f"Boot self-heal check failed: {e}", level="DEBUG")

        self.log(f"DryerMonitor (Miele TCB150 WP) initialized - state: {self.state}", level="INFO")

    def _now_utc(self):
        return datetime.fromtimestamp(time.time(), timezone.utc)

    def _local_tz(self):
        return datetime.now().astimezone().tzinfo

    def _format_local(self, dt):
        if dt is None:
            return ""
        tz = self._local_tz()
        return dt.astimezone(tz).isoformat(timespec="seconds") if getattr(dt, "tzinfo", None) else str(dt)

    def _format_utc(self, dt):
        if dt is None:
            return ""
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _parse_utc_iso(self, s):
        """Parse ISO datetime string from entity attribute to UTC datetime."""
        if not s or not isinstance(s, str):
            return None
        try:
            s = s.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    def _restore_remaining_seconds(self, total_seconds, since, floor_s=60):
        """Seconds left until total_seconds have elapsed since `since` (UTC), floored at
        floor_s so a restore never arms a timer with ~0 delay (or a negative one, if the
        period already ran out while AppDaemon was down)."""
        elapsed = (self._now_utc() - since).total_seconds()
        return max(floor_s, int(total_seconds - elapsed))

    def _boot_full_state_snapshot(self):
        """The boot-time full-state snapshot captured once in initialize() (see the
        boot-resolution comment there), or - when called directly, as several tests do,
        without initialize() ever having run - a live read. Never the live entity when the
        snapshot exists: by the time any _restore_* method runs in initialize(), the entity
        has already been republished once (the first write, see _set_state_entity), and after
        a HA-restart erasure that republish creates it with no attributes at all (the
        2026-07-27 bug this file's tests are named for)."""
        if hasattr(self, "_boot_full_state"):
            return self._boot_full_state
        return self.get_state(self.state_entity, attribute="all")

    def _boot_store_snapshot(self):
        """The on-disk store payload loaded once in initialize() (see the boot-resolution
        comment there), or {} when called directly without initialize() having run."""
        data = self._boot_store_data if hasattr(self, "_boot_store_data") else None
        return data if isinstance(data, dict) else {}

    def _restore_running_state(self):
        """After AppDaemon restart: restore start_time and timers when state is Running/Paused.
        Ensures we keep polling power and can detect cycle end (power drop), and re-arms the
        watchdogs that would otherwise stay disarmed until the next full cycle: the 5h running
        watchdog spans Running AND any Paused time within it (it is never cancelled on pause -
        see _transition_to_paused), so both restored states re-arm it here; Paused additionally
        re-arms its own pause timeout.

        Entity attributes win when present (AD-only-reload path, unchanged); the on-disk store
        (cycle_store.py) fills anything the entity does not have - which, after a HA-restart
        erasure, is everything, and for a few fields (max_power_w, door_opened_during_cycle,
        keep_fresh_detected, high_power_counter, notification_sent) is always true, since
        _update_running_attributes never publishes those to the entity at all."""
        if self.state not in ("Running", "Paused"):
            return
        try:
            full = self._boot_full_state_snapshot()
            attrs = (full.get("attributes") or {}) if isinstance(full, dict) else {}
            store = self._boot_store_snapshot()

            cycle_start = attrs.get("cycle_start_time") or store.get("cycle_start_time")
            if cycle_start:
                self.start_time = parse_utc(cycle_start)
            if self.start_time is None:
                self.log("Restore Running: no cycle_start_time from entity or store - start_time left None", level="WARNING")
            else:
                en = attrs.get("energy_at_start")
                if en is None:
                    en = store.get("energy_at_start")
                if en is not None:
                    try:
                        self.energy_start = float(en)
                    except (ValueError, TypeError):
                        pass
                prog = attrs.get("detected_programme") or store.get("detected_programme") or "unknown"
                self.detected_programme = prog
                # Captured HERE, before the _update_running_attributes() call a few lines below
                # (in this same method) overwrites self.detected_programme with a live
                # reclassification from elapsed+energy - see the boot self-heal in initialize(),
                # which needs what was actually restored, not that later reclassification.
                self._restored_detected_programme = prog
                dur = attrs.get("programme_duration_min")
                if dur is None:
                    dur = store.get("programme_duration_min")
                if dur is not None:
                    try:
                        self.expected_dur_at_start = float(dur)
                    except (ValueError, TypeError):
                        pass
                if self.expected_dur_at_start is None:
                    self.expected_dur_at_start = self._get_guard_duration(tick_prog=prog)
                # Store-only fields below - never published to the entity, so a HA-restart
                # erasure has no other source for them at all.
                max_power_w = store.get("max_power_w")
                if max_power_w is not None:
                    try:
                        self.max_power_w = float(max_power_w)
                    except (ValueError, TypeError):
                        pass
                if "door_opened_during_cycle" in store:
                    self.door_opened_during_cycle = bool(store.get("door_opened_during_cycle"))
                door_opened_time = store.get("door_opened_time")
                if door_opened_time:
                    self.door_opened_time = parse_utc(door_opened_time)
                if "keep_fresh_detected" in store:
                    self.keep_fresh_detected = bool(store.get("keep_fresh_detected"))
                high_power_counter = store.get("high_power_counter")
                if high_power_counter is not None:
                    try:
                        self.high_power_counter = int(high_power_counter)
                    except (ValueError, TypeError):
                        pass
                if "notification_sent" in store:
                    self.notification_sent = bool(store.get("notification_sent"))
                if store.get("cycle_id"):
                    self.cycle_id = store.get("cycle_id")
            if not self.poll_timer:
                self.poll_timer = self.run_in(self._poll_power, 60)
            if not self.classify_timer:
                self.classify_timer = self.run_in(self._tick_classify, 30)
            if self.start_time:
                self._update_running_attributes()
                self._safe_cancel_timer(self.running_watchdog_timer)
                remaining = self._restore_remaining_seconds(
                    int(self.max_running_hours * 3600), self.start_time, floor_s=60
                )
                self.running_watchdog_timer = self.run_in(self._running_watchdog_timeout, remaining)
            if self.state == "Paused":
                last_changed = (
                    (full.get("last_changed") or full.get("last_updated")) if isinstance(full, dict) else None
                ) or store.get("state_since")
                pause_started = parse_utc(last_changed) if last_changed else None
                total_pause_s = self.pause_timeout_minutes * 60
                if pause_started:
                    remaining_pause = self._restore_remaining_seconds(total_pause_s, pause_started, floor_s=60)
                else:
                    self.log("Restore Paused: no last_changed on entity or store - arming full pause timeout", level="DEBUG")
                    remaining_pause = total_pause_s
                self._safe_cancel_timer(self.pause_timer)
                self.pause_timer = self.run_in(self._pause_timeout, remaining_pause)
            self.log("Restored Running state: start_time and timers resumed", level="INFO")
        except Exception as e:
            self.log(f"Restore Running state failed: {e}", level="WARNING")

    def _restore_unemptied_state(self):
        """After AppDaemon restart: re-arm the Unemptied auto-clear watchdog from the time
        Unemptied actually began (state entity last_changed, falling back to the store's
        state_since), falling back to the full period when neither is derivable. Without this
        the watchdog fallback is lost across every deploy while Unemptied (the door-open
        listener is unaffected and still works)."""
        try:
            full = self._boot_full_state_snapshot()
            store = self._boot_store_snapshot()
            last_changed = (
                (full.get("last_changed") or full.get("last_updated")) if isinstance(full, dict) else None
            ) or store.get("state_since")
            unemptied_since = parse_utc(last_changed) if last_changed else None
            total_s = int(self.unemptied_timeout_hours * 3600)
            if unemptied_since:
                remaining = self._restore_remaining_seconds(total_s, unemptied_since, floor_s=60)
            else:
                self.log("Restore Unemptied: no last_changed on entity or store - arming full unemptied watchdog", level="DEBUG")
                remaining = total_s
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = self.run_in(self._unemptied_watchdog_timeout, remaining)
            self.log(f"Restored Unemptied state: watchdog re-armed ({remaining}s remaining)", level="INFO")
        except Exception as e:
            self.log(f"Restore Unemptied state failed: {e}", level="WARNING")

    def _restore_emptied_state(self):
        """After AppDaemon restart: re-arm the Emptied auto-clear watchdog from the time Emptied
        actually began (state entity last_changed, falling back to the store's state_since),
        falling back to the full period when neither is derivable. Mirrors
        _restore_unemptied_state. AppDaemon restarts on every deploy, and Emptied is only ever
        left by a door-close event, so without this re-arm a deploy that lands while Emptied
        leaves the state with no timer and no way out."""
        try:
            full = self._boot_full_state_snapshot()
            store = self._boot_store_snapshot()
            last_changed = (
                (full.get("last_changed") or full.get("last_updated")) if isinstance(full, dict) else None
            ) or store.get("state_since")
            emptied_since = parse_utc(last_changed) if last_changed else None
            total_s = int(self.emptied_timeout_minutes * 60)
            if emptied_since:
                remaining = self._restore_remaining_seconds(total_s, emptied_since, floor_s=60)
            else:
                self.log("Restore Emptied: no last_changed on entity or store - arming full emptied watchdog", level="DEBUG")
                remaining = total_s
            self._safe_cancel_timer(self.emptied_watchdog_timer)
            self.emptied_watchdog_timer = self.run_in(self._emptied_watchdog_timeout, remaining)
            self.log(f"Restored Emptied state: watchdog re-armed ({remaining}s remaining)", level="INFO")
        except Exception as e:
            self.log(f"Restore Emptied state failed: {e}", level="WARNING")

    def _get_current_power(self):
        """Get current power reading in watts."""
        try:
            power_state = self.get_state(self.power_sensor)
            if power_state not in ["unknown", "unavailable", None]:
                return float(power_state)
        except (ValueError, TypeError):
            pass
        return 0

    def _get_run_duration_minutes(self):
        """Get how long the current cycle has been running in minutes."""
        if self.start_time is None:
            return 0
        return (self._now_utc() - self.start_time).total_seconds() / 60

    def _get_energy_used(self):
        """Get energy consumed since cycle start in kWh."""
        if self.energy_start is None:
            return 0
        try:
            current_energy = self.get_state(self.energy_sensor)
            if current_energy is not None and current_energy not in ["unknown", "unavailable"]:
                return float(current_energy) - self.energy_start
        except (ValueError, TypeError):
            pass
        return 0

    def _is_valid_completed_cycle(self):
        """Check if the cycle ran long enough and used enough energy."""
        run_minutes = self._get_run_duration_minutes()
        energy_used = self._get_energy_used()

        # Keep-fresh detection counts as valid
        if self.keep_fresh_detected:
            return True

        is_valid = (run_minutes >= self.min_cycle_minutes and
                    energy_used >= self.min_energy_kwh)

        self.log(f"Cycle validation: {run_minutes:.1f} min (need {self.min_cycle_minutes}), "
                 f"{energy_used:.3f} kWh (need {self.min_energy_kwh}) -> {'valid' if is_valid else 'invalid'}",
                 level="DEBUG")

        return is_valid

    def _get_profile(self, programme: str):
        """Return profile dict for programme."""
        return DryerMonitor.PROGRAMME_PROFILES.get(
            programme, DryerMonitor.PROGRAMME_PROFILES.get("unknown", {})
        )

    def _load_programme_profiles(self):
        """Load programme profiles from dryer_programmes.yaml. Minimal defaults; all keys from YAML."""
        prog_file = self.args.get("programmes_file") or os.path.join(
            os.path.dirname(__file__), "dryer_programmes.yaml"
        )
        defaults = {
            "unknown": {"label": "Unknown", "duration_min": 120, "max_energy_kwh": 2.0},
            "finish_uld": {"label": "Finish uld", "duration_min": 5, "max_energy_kwh": 0.02, "is_real": False},
        }
        try:
            with open(prog_file, "r") as f:
                data = yaml.safe_load(f) or {}
            profiles = data.get("programmes", {})
            if profiles:
                merged = {**defaults}
                for key, val in profiles.items():
                    if isinstance(val, dict):
                        merged[key] = {**merged.get(key, {}), **val}
                DryerMonitor.PROGRAMME_PROFILES = merged
                self.log(f"Loaded {len(profiles)} programme profiles from {prog_file}", level="INFO")
            else:
                DryerMonitor.PROGRAMME_PROFILES = defaults
        except FileNotFoundError:
            DryerMonitor.PROGRAMME_PROFILES = defaults
            self.log(f"Programme file {prog_file} not found - using defaults", level="WARNING")
        except Exception as exc:
            DryerMonitor.PROGRAMME_PROFILES = defaults
            self.log(f"Failed to load {prog_file}: {exc} - using defaults", level="ERROR")

    def _load_and_apply_feedback(self):
        """Load dryer_feedback.json and apply learned programme data. Only confirmed cycles."""
        self._learned_durations = {}
        if not os.path.exists(self.feedback_file):
            return
        try:
            with open(self.feedback_file, "r") as f:
                data = __import__("json").load(f)
        except Exception as e:
            self.log(f"Could not load feedback {self.feedback_file}: {e}", level="WARNING")
            return
        cycles = data.get("cycles") or []
        for c in cycles:
            if not c.get("programme_confirmed_by_human", c.get("programme_user_confirmed", False)):
                continue
            prog = c.get("confirmed", "")
            if not prog or prog == "unknown":
                continue
            dur = c.get("duration_min")
            if dur is None or dur <= 0:
                continue
            key = prog
            prev = self._learned_durations.get(key, {"n": 0, "avg": float(dur)})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + float(dur)) / n_new
            self._learned_durations[key] = {"n": n_new, "avg": avg_new}
        if self._learned_durations:
            self.log(f"Loaded learned durations for {list(self._learned_durations.keys())}", level="INFO")

    def _classify_programme(self):
        """Classify programme from energy + runtime (heat pump: use energy and time, not power bands)."""
        if not self.start_time:
            return "unknown"
        run_min = (self._now_utc() - self.start_time).total_seconds() / 60
        energy = self._get_energy_used()
        if run_min < 5:
            return "unknown"
        # Finish uld: ~0.02 kWh, ~5 min - not a real cycle
        if energy < 0.05 and run_min < 10:
            return "finish_uld"
        # Short real cycles: 0.46–0.50 kWh, 60–66 min (skjorter, strygelet, finvask)
        if 0.40 <= energy <= 0.55 and 55 <= run_min <= 75:
            if run_min <= 62 and energy <= 0.48:
                return "skjorter__skabstoert"
            if run_min >= 64:
                return "strygelet__skabstoert"
            return "finvask__skabstoert__skane_fixed"
        # Medium: 0.75–0.96 kWh (imprægnering 95 min, ekspres 100 min)
        if 0.70 <= energy <= 1.0 and 90 <= run_min <= 105:
            if energy <= 0.78 and run_min <= 97:
                return "impraegnering__skabstoert"
            return "ekspres__skabstoert"
        # Denim: ~0.95 kWh, ~115 min
        if 0.88 <= energy <= 1.05 and 108 <= run_min <= 125:
            return "denim__skabstoert"
        # Bomuld strygetørt: 0.85–1.25 kWh, 83–120 min
        if 0.80 <= energy <= 1.30 and 80 <= run_min <= 125:
            return "bomuld__strygetoert"
        # Bomuld skabstørt: 1.30–1.50 kWh, 118–140 min
        if 1.25 <= energy <= 1.55 and 115 <= run_min <= 145:
            return "bomuld__skabstoert"
        # Bomuld skabstørt + Skåne: 1.75 kWh, 165 min
        if energy >= 1.65 and run_min >= 155:
            return "bomuld__skabstoert__skane"
        # Bomuld eco: 1.70 kWh, 155 min
        if energy >= 1.55 and run_min >= 140:
            return "bomuld_eco__skabstoert"
        if 0.90 <= energy <= 1.0 and run_min >= 90 and run_min <= 100:
            return "ekspres__skabstoert"
        # Fallbacks by energy
        if energy < 0.5 and run_min < 70:
            return "skjorter__skabstoert"
        if energy < 0.9 and run_min < 110:
            return "ekspres__skabstoert"
        if energy < 1.2:
            return "bomuld__strygetoert"
        if energy < 1.6:
            return "bomuld__skabstoert"
        return "bomuld_eco__skabstoert"

    def _get_programme_duration(self, prog: str, use_learned: bool = True) -> int:
        """Expected duration in minutes. For guards use use_learned=False."""
        profile = self._get_profile(prog)
        manual = profile.get("duration_min")
        if manual is None or manual <= 0:
            manual = 120
        if not use_learned:
            return manual
        learned = self._learned_durations.get(prog)
        if learned is None or learned["n"] < 1:
            return manual
        n, avg = learned["n"], learned["avg"]
        if n == 1:
            return round(0.30 * avg + 0.70 * manual)
        if n == 2:
            return round(0.50 * avg + 0.50 * manual)
        alpha = min(0.9, 0.6 + (n - 3) * (0.30 / 7))
        return round(alpha * avg + (1 - alpha) * manual)

    def _programme_max_duration_minutes(self, classification=None):
        prog = classification or self._classify_programme()
        profile = self._get_profile(prog)
        dur = profile.get("duration_min")
        if dur is None or dur <= 0:
            return int(self.max_running_hours * 60)
        return dur

    def _get_guard_duration(self, tick_prog=None):
        """Duration for 80% guard. Use manual duration only."""
        if tick_prog and tick_prog != "unknown":
            d = self._get_programme_duration(tick_prog, use_learned=False)
            if d:
                return d
        if self.expected_dur_at_start is not None:
            return self.expected_dur_at_start
        return self._programme_max_duration_minutes(classification=tick_prog)

    @staticmethod
    def _overrun_eta(elapsed, dur, keep_fresh, current_power, main_cycle_power, overrun_floor_min):
        """Pure overrun-aware remaining/progress/overrun computation, extracted from
        _update_running_attributes so it is directly unit-testable without an AppDaemon app
        instance.

        Not yet overrun (dur - elapsed > 0): unchanged passthrough -- remaining is the plain
        dur-elapsed and progress is the usual elapsed/dur percentage.

        Once overrun, the old remaining=0/progress=100 was indistinguishable from "actually
        done" and got silently dropped from the published attributes entirely (AppDaemon
        4.5.13 set_state bug -- see the comment in _update_running_attributes). Instead, ask
        what the dryer's own signals say:
          - keep_fresh_detected (anti-crease/keep-fresh pattern) -> main cycle is genuinely
            done, finish flow imminent -> remaining=2, progress=100.
          - current_power >= main_cycle_power -> still heating/drying hard, provably not
            done -> remaining floors at overrun_floor_min minutes, progress=99.
          - otherwise (power low/intermittent but the keep-fresh pattern isn't confirmed yet)
            -> remaining=5, progress=99.

        Returns (remaining_min, progress_pct, overrun_min). overrun_min is always >= 0 (0
        when not overrun).
        """
        raw_remaining = dur - elapsed
        if raw_remaining > 0:
            progress = min(100, round(100 * elapsed / dur)) if dur > 0 else 0
            return raw_remaining, progress, 0
        overrun = -raw_remaining
        if keep_fresh:
            return 2, 100, overrun
        if current_power >= main_cycle_power:
            return overrun_floor_min, 99, overrun
        return 5, 99, overrun

    def _update_running_attributes(self):
        """Update state entity with progress/ETA attributes while Running."""
        if self.get_state(self.state_entity) not in ("Running", "Paused") or not self.start_time:
            return
        prog = self._classify_programme()
        self.detected_programme = prog
        # Effective key: user-selected (derived from selectors) or classified
        effective_key = self._get_confirmed_programme_key() or prog
        profile = self._get_profile(effective_key)
        label = profile.get("label", effective_key)
        dur = self._get_programme_duration(effective_key)
        now = self._now_utc()
        elapsed = (now - self.start_time).total_seconds() / 60
        overrun = 0
        if dur > 0:
            current_power = self._get_current_power()
            remaining, progress, overrun = self._overrun_eta(
                elapsed, dur, self.keep_fresh_detected, current_power,
                self.main_cycle_power, self.overrun_floor_min,
            )
            eta = (self.start_time + timedelta(minutes=dur)) if overrun <= 0 else (now + timedelta(minutes=remaining))
        else:
            remaining, progress, eta = 0, 0, None
        attrs = {
            "detected_programme": prog,
            "derived_programme_key": effective_key,
            "programme_label": label,
            "cycle_start_time": self._format_utc(self.start_time),
            "started_at_display": self._format_local(self.start_time),
            "programme_duration_min": dur,
            "elapsed_minutes": round(elapsed, 1),
            "progress_pct": progress,
            # NEVER 0 -- see the AppDaemon 4.5.13 zero-drop note below.
            "estimated_remaining_min": max(1, round(remaining)),
            "estimated_end_time": self._format_utc(eta) if eta else None,
            "energy_at_start": self.energy_start,
            "energy_used": round(self._get_energy_used(), 3),
        }
        if overrun > 0:
            attrs["overrun_min"] = round(overrun, 1)
        try:
            full = self.get_state(self.state_entity, attribute="all")
            existing = dict((full or {}).get("attributes") or {})
            existing.update(attrs)
            if overrun <= 0:
                # attrs merges over the previous publish, so a past overrun_min would otherwise
                # linger after dur grows mid-cycle (selector/classification change) and leak a
                # phantom "+X min over" into later ticks. Absent == no overrun for the card.
                existing.pop("overrun_min", None)
            # elapsed_minutes/progress_pct/energy_used silently drop from published attributes
            # whenever they're 0/0.0 (every cycle's first tick(s)) -- AppDaemon 4.5.13 set_state
            # bug, not ours; see smart_cooling.py's _publish() for details. estimated_remaining_min
            # is now always published as max(1, ...) so it can never hit that zero-drop path, even
            # deep into an overrun. overrun_min is only in attrs when overrun > 0 and popped from
            # the merge otherwise, so it never depends on the zero-drop behavior at all.
            self._set_state_entity( state=self.get_state(self.state_entity), attributes=existing, replace=True)
        except Exception:
            pass

    def _attrs_clear_running_progress(self):
        """Clear Running-cycle progress fields so the UI does not show stale ETA/progress after Unemptied/Emptied.
        Match washer: use \"\" / None so HA replaces values (partial updates can leave old attrs)."""
        return {
            "cycle_start_time": "",
            "started_at_display": "",
            "estimated_end_time": "",
            "estimated_remaining_min": None,
            "elapsed_minutes": None,
            "progress_pct": None,
            "programme_duration_min": None,
            "programme_label": "",
            "detected_programme": "",
            "energy_at_start": None,
            "overrun_min": None,
        }

    def _tick_classify(self, kwargs):
        """Periodic classification and attribute update."""
        if self.get_state(self.state_entity) not in ("Running", "Paused"):
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None
            return
        self._update_running_attributes()
        self.classify_timer = self.run_in(self._tick_classify, 30)

    def _flatten_history(self, hist, entity_id=None):
        """AppDaemon get_history returns list[list[dict]]. Normalize to list[dict]."""
        if isinstance(hist, dict):
            hist = hist.get(entity_id or "", []) or hist.get("history", []) if entity_id else next(iter(hist.values()), [])
        if isinstance(hist, list) and hist and isinstance(hist[0], list):
            return hist[0]
        return hist if isinstance(hist, list) else []

    def _get_programme_duration_hint_for_history(self):
        """Duration hint for history correction: detected programme."""
        if self.detected_programme and self.detected_programme != "unknown":
            return float(self._get_programme_duration(self.detected_programme))
        return None

    def _estimate_cycle_end_from_history(self, expected_duration_min=None):
        """Estimate when cycle ended from HA energy history."""
        if not self.start_time or not self.energy_sensor:
            return None
        try:
            end_time = self._now_utc()
            hist = self.get_history(entity_id=self.energy_sensor, start_time=self.start_time, end_time=end_time)
            hist = self._flatten_history(hist, self.energy_sensor)
            if len(hist) < 2:
                return None
            points = []
            for entry in hist:
                try:
                    ts = entry.get("last_changed") or entry.get("last_updated")
                    if not ts:
                        continue
                    t = self._parse_utc(ts)
                    if t is None:
                        continue
                    s = entry.get("state")
                    if s is None or s in ("unknown", "unavailable", ""):
                        continue
                    points.append((t, float(s)))
                except (ValueError, TypeError):
                    continue
            if len(points) < 2:
                return None
            points.sort(key=lambda x: x[0])
            run_minutes_max = (end_time - self.start_time).total_seconds() / 60
            high_end_candidates = []
            for i in range(1, len(points)):
                t1, e1 = points[i - 1]
                t2, e2 = points[i]
                delta_s = (t2 - t1).total_seconds()
                if delta_s < 10:
                    continue
                implied_w = ((e2 - e1) * 1000) / (delta_s / 3600)
                if implied_w > self.energy_active_watts:
                    high_end_candidates.append(t2)
            if not high_end_candidates:
                return None
            if expected_duration_min and expected_duration_min > 0:
                best_end, best_diff = None, float("inf")
                for t in high_end_candidates:
                    dur = (t - self.start_time).total_seconds() / 60
                    if dur < self.min_cycle_minutes or dur > run_minutes_max:
                        continue
                    diff = abs(dur - expected_duration_min)
                    if diff < best_diff:
                        best_diff, best_end = diff, t
                if best_end:
                    return best_end + timedelta(minutes=2)
            last = high_end_candidates[-1]
            return last + timedelta(minutes=2)
        except Exception as e:
            self.log(f"Could not estimate cycle end from history: {e}", level="DEBUG")
            return None

    def _parse_utc(self, s: str):
        """Parse ISO timestamp to timezone-aware UTC datetime."""
        if not s:
            return None
        s = str(s).strip().replace("Z", "+00:00")
        if "+" not in s[-7:] and "-" not in s[-7:]:
            s = s + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    def _correct_duration(self, run_minutes_wall: float, log_prefix: str = "") -> tuple:
        """Correct wall-clock duration using HA history. Returns (run_minutes, duration_source)."""
        run_minutes = run_minutes_wall
        duration_source = None
        hint = self._get_programme_duration_hint_for_history()
        actual_end = self._estimate_cycle_end_from_history(expected_duration_min=hint)
        if actual_end and self.start_time:
            run_minutes_actual = (actual_end - self.start_time).total_seconds() / 60
            if run_minutes_actual >= self.min_cycle_minutes and run_minutes_actual <= run_minutes:
                delta = run_minutes - run_minutes_actual
                if delta > 1.0:
                    self.log(f"{log_prefix}Using HA history: {run_minutes_actual:.1f} min (detection was {delta:.0f} min late)", level="INFO")
                run_minutes = run_minutes_actual
                duration_source = "history_corrected"
        return (run_minutes, duration_source)

    # Dryness selector value -> internal segment (for derived key)
    _DRYNESS_TO_SEGMENT = {
        "Ekstra tørt": "ekstra_toert",
        "Skabstørt": "skabstoert",
        "Strygetørt": "strygetoert",
        "Rulletørt": "rulletoert",
    }
    # Programmes where user must pick a real dryness (not "—", same convention as washer sub-selectors)
    _PROGRAMMES_REQUIRING_DRYNESS = frozenset(
        ("Finvask", "Udglatning", "Bomuld", "Strygelet", "Skjorter", "Denim", "Ekspres", "Sengetøj")
    )

    def _get_confirmed_programme_key(self) -> str | None:
        """Derive flat programme key from split selectors (programme, dryness, Skåne+, time).
        Returns None if programme is 'Auto (unconfirmed)', dryness is not a real level (when required;
        use '—' like washer temperature/spin), or selectors not configured."""
        if not self.programme_entity:
            return None
        prog = (self.get_state(self.programme_entity) or "").strip()
        if not prog or prog in ("Auto (unconfirmed)", "unknown"):
            return None
        dryness = (self.get_state(self.dryness_entity) or "").strip() if self.dryness_entity else ""
        skane_on = self.get_state(self.skane_plus_entity) == "on" if self.skane_plus_entity else False
        time_val = (self.get_state(self.time_minutes_entity) or "").strip() if self.time_minutes_entity else ""

        # Fixed programmes (no dryness / time choice)
        if prog in ("Bomuld eco", "Bomuld eco - Skabstørt"):
            return "bomuld_eco__skabstoert"
        if prog in ("Finish uld",):
            return "finish_uld"
        if prog in ("Imprægnering",):
            return "impraegnering__skabstoert"

        # Varm luft: time 20–120 in 10-min steps
        if prog in ("Varm luft",):
            try:
                # Allow "90", "90 min", etc.
                digits = "".join(c for c in str(time_val or "90") if c.isdigit())
                min_val = int(digits) if digits else 90
                min_val = max(20, min(120, ((min_val + 5) // 10) * 10))
            except (ValueError, TypeError):
                min_val = 90
            seg = f"varm_luft__{min_val:03d}min"
            return f"{seg}__skane" if skane_on else seg

        if prog not in self._PROGRAMMES_REQUIRING_DRYNESS:
            return None

        # Only the four real levels count (same idea as washer: "—" is not in allowed list)
        if not dryness or dryness not in self._DRYNESS_TO_SEGMENT:
            return None
        dry_seg = self._DRYNESS_TO_SEGMENT[dryness]

        # Programmes with Skåne+ always on (Finvask, Udglatning)
        if prog in ("Finvask",):
            if dry_seg == "strygetoert":
                return "finvask__strygetoert__skane_fixed"
            return "finvask__skabstoert__skane_fixed"
        if prog in ("Udglatning",):
            if dry_seg == "strygetoert":
                return "udglatning__strygetoert__skane_fixed"
            return "udglatning__skabstoert__skane_fixed"

        # Programmes with optional Skåne+ and dryness
        base = None
        if prog in ("Bomuld",):
            base = "bomuld"
        elif prog in ("Strygelet",):
            base = "strygelet"
        elif prog in ("Skjorter",):
            base = "skjorter"
        elif prog in ("Denim",):
            base = "denim"
        elif prog in ("Ekspres",):
            base = "ekspres"
            skane_on = False  # Ekspres has no Skåne+
        elif prog in ("Sengetøj",):
            base = "sengetoej"
            skane_on = False  # Sengetøj has no Skåne+
        if base:
            key = f"{base}__{dry_seg}"
            if skane_on:
                key += "__skane"
            return key

        return None

    def _get_confirmed_from_selector(self, predicted: str) -> tuple:
        """Return (confirmed_programme_key, programme_confirmed_by_human). Uses derived key from split selectors."""
        key = self._get_confirmed_programme_key()
        if key is None:
            return (predicted, False)
        return (key, True)

    def _save_cycle_feedback(
        self,
        predicted: str,
        confirmed: str,
        duration_min: float,
        energy_kwh: float,
        max_power_w: float,
        programme_confirmed_by_human: bool = False,
        duration_source: str | None = None,
        end_reason: str | None = None,
        idle_min: float | None = None,
    ):
        """Append one cycle record to dryer_feedback.json.

        Defence in depth (2026-08-12 finish_uld duplicate-feedback bug): every call site already
        gates this call on _transition_to_unemptied's return value, but this second, independent
        check refuses a second save for the SAME cycle_id even if some future call site forgets
        that gate. cycle_id is minted fresh only when a new Running cycle actually begins
        (_confirm_running) and otherwise carried forward untouched - see its own comment - so
        equality here means "this exact physical cycle already has a record."
        """
        # getattr, not a direct attribute read: some callers - this repo's own lighter-weight
        # test harnesses among them (test_dryer_pause_exit.py's make_app never runs the real
        # initialize()) - construct a DryerMonitor with cycle_id never set at all. Falling back
        # to None there just degrades to "no guard" for that instance, exactly how every call
        # site behaved before this fix existed - never a new way for this method to raise.
        current_cycle_id = getattr(self, "cycle_id", None)
        if current_cycle_id is not None and current_cycle_id == getattr(self, "_feedback_saved_cycle_id", None):
            self.log(
                f"Duplicate _save_cycle_feedback refused for cycle_id {current_cycle_id} - a "
                f"record was already saved for this cycle",
                level="DEBUG",
            )
            return
        import json
        record = {
            "ts": self._format_local(self._now_utc()),
            "duration_min": round(duration_min, 1),
            "energy_kwh": round(energy_kwh, 3),
            "predicted": predicted,
            "confirmed": confirmed,
            "programme_confirmed_by_human": programme_confirmed_by_human,
            "max_power_w": round(max_power_w, 0),
        }
        if duration_source:
            record["duration_source"] = duration_source
        if end_reason:
            record["end_reason"] = end_reason
        if idle_min is not None and idle_min >= 0:
            record["idle_min"] = round(idle_min, 1)
        data = {"version": 2, "cycles": []}
        if os.path.exists(self.feedback_file):
            try:
                with open(self.feedback_file, "r") as f:
                    data = json.load(f)
            except Exception:
                pass
        data["version"] = 2
        data.setdefault("cycles", []).append(record)
        try:
            with open(self.feedback_file, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"Could not write feedback {self.feedback_file}: {e}", level="WARNING")
            return
        self._feedback_saved_cycle_id = current_cycle_id  # mark this cycle done - see the guard above
        if programme_confirmed_by_human:
            prev = self._learned_durations.get(confirmed, {"n": 0, "avg": duration_min})
            n_new = prev["n"] + 1
            avg_new = (prev["avg"] * prev["n"] + duration_min) / n_new
            self._learned_durations[confirmed] = {"n": n_new, "avg": avg_new}
        self.log(f"Feedback saved: {confirmed} duration {duration_min:.0f}min energy {energy_kwh:.2f}kWh", level="INFO")

    def _should_change_state(self, new_state, force=False):
        """Check if we should allow a state change.

        UTC, never naive local time. At the October DST fall-back the local clock jumps an hour
        BACKWARDS, so a naive `now - last_state_change` goes negative for the whole repeated hour
        - and a negative delta is trivially < cooling_period, which refuses every un-forced
        transition until the hour is out. Same silent-refusal wedge the door paths were fixed for,
        except it hits every un-forced path at once, once a year.

        Every writer of last_state_change must stay on _now_utc() for the same reason the
        comparison does: mixing a naive stamp with an aware `now` raises TypeError on the
        subtraction. See washer_monitor.py's _should_change_state, which has always been UTC."""
        if self.get_state(self.state_entity) == new_state:
            return False
        now = self._now_utc()
        if not force and self.last_state_change and (now - self.last_state_change).total_seconds() < self.cooling_period:
            self.log(f"In cooling period", level="DEBUG")
            return False
        self.last_state_change = now
        self.state_since = now  # store-persistable twin of last_state_change - see its init comment
        return True

    def _record_power_reading(self, watts):
        """Record power readings to detect patterns"""
        self.power_readings.append(watts)
        if len(self.power_readings) > self.pattern_window:
            self.power_readings.pop(0)

    def _door_state_changed(self, entity, attr, old, new, kwargs):
        """Handle door open and close events."""
        current_state = self.get_state(self.state_entity)

        if new == "on":  # Door opened
            self._handle_door_opened(current_state)
        elif new == "off":  # Door closed
            self._handle_door_closed(current_state)

    def _handle_door_opened(self, current_state):
        """Handle door opening. Fill window: before fill_window_minutes, door+low power = Off (add laundry)."""
        current_power = self._get_current_power()
        elapsed = self._get_run_duration_minutes()
        self.log(f"Door opened, state: {current_state}, power: {current_power:.1f}W, elapsed: {elapsed:.1f}min", level="DEBUG")

        if current_state in ("Off", "Emptied"):
            self.door_fast_start_armed_until = None

        if current_state == "Running":
            if current_power <= self.stop_w or self.keep_fresh_detected:
                # Low power - cycle finished or interrupted
                if elapsed < self.fill_window_minutes:
                    self.log(f"Door opened with low power before {self.fill_window_minutes}min - Off (add laundry/interrupted)", level="INFO")
                    self._transition_to_off("Door opened before fill window - add laundry or interrupted")
                elif not self._is_valid_completed_cycle():
                    self._transition_to_off("Door opened - cycle invalid or incomplete")
                else:
                    prog = self._classify_programme()
                    effective_key = self._get_confirmed_programme_key() or prog
                    guard_dur = self._get_guard_duration(tick_prog=effective_key)
                    if elapsed < guard_dur * 0.8:
                        self.log(f"Door open: run {elapsed:.0f}min < 80% of {guard_dur:.0f}min - treating as incomplete", level="DEBUG")
                        self._transition_to_off("Door opened - cycle not past 80% guard")
                    else:
                        self.log(f"Door opened with low power ({current_power:.1f}W) - user is emptying -> Unemptied then Emptied", level="INFO")
                        run_minutes_wall = elapsed
                        run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Door-open ")
                        idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                        energy_kwh = self._get_energy_used()
                        became_unemptied = self._transition_to_unemptied(skip_announce=True, run_minutes=run_minutes, energy_used=energy_kwh)
                        if became_unemptied:
                            confirmed, is_human = self._get_confirmed_from_selector(prog)
                            self._save_cycle_feedback(
                                predicted=prog,
                                confirmed=confirmed,
                                duration_min=run_minutes,
                                energy_kwh=energy_kwh,
                                max_power_w=self.max_power_w,
                                programme_confirmed_by_human=is_human,
                                duration_source=duration_source,
                                end_reason="door_opened_first",
                                idle_min=idle_min,
                            )
                        self._transition_to_emptied("Door opened after cycle complete - emptying")
            else:
                # Power is HIGH - user is checking laundry mid-cycle
                self.door_opened_time = datetime.now()
                self.door_opened_during_cycle = True
                self._transition_to_paused()

        elif current_state == "Paused":
            self.door_opened_time = datetime.now()

        elif current_state == "Unemptied":
            # Door opened after notification - user is emptying
            self._transition_to_emptied("Door opened - emptying")

    def _handle_door_closed(self, current_state):
        """Handle door closing event."""
        self.log(f"Door closed, current state: {current_state}", level="DEBUG")

        if current_state == "Paused":
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None

            current_power = self._get_current_power()

            if current_power >= self.start_w:
                # Door-close from Paused is authoritative and may happen seconds after
                # Paused was entered; bypass cooling so we don't get stuck in Paused.
                self._transition_to_running_from_pause(force=True)
            else:
                self._evaluate_pause_exit(force=True)
        
        elif current_state == "Emptied":
            # Door closed after emptying - cycle complete, go to Off
            self.log(f"Door closed after emptying -> Off", level="INFO")
            # _transition_to_emptied stamped the cooling clock seconds ago (it forces its own
            # transition), so the door-close that follows almost always lands inside the 600s
            # window: 52 of 92 of these since January were silently refused, leaving Emptied
            # terminal and blocking the next cycle (start detection requires "Off").
            # A door event is discrete and physical - never what cooling_period protects against.
            self._transition_to_off("Door closed - emptying complete", force=True)
            now = self._now_utc()
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

        elif current_state == "Off":
            now = self._now_utc()
            self.door_fast_start_armed_until = now + timedelta(seconds=self.door_close_fast_start_window_s)

    def _transition_to_paused(self):
        """Transition to Paused state when door opens during Running with high power.

        Returns True/False from _should_change_state - same silent-refusal shape as
        _transition_to_unemptied (see its docstring for why that return value matters there);
        no caller here currently needs it.
        """
        if self._should_change_state("Paused"):
            self.state = "Paused"
            # keep_fresh_detected silently drops from published attributes whenever it's False
            # (always False on this path -- Paused excludes the keep-fresh branch) -- AppDaemon
            # 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self._set_state_entity(
                state="Paused",
                attributes={
                    "reason": "Door opened during cycle (checking laundry)",
                    "run_time_minutes": round(self._get_run_duration_minutes(), 1),
                    "energy_used": round(self._get_energy_used(), 3),
                    "keep_fresh_detected": self.keep_fresh_detected
                }
            )
            self.log(f"State -> Paused (checking laundry mid-cycle)", level="INFO")

            # Timeout
            self.pause_timer = self.run_in(
                self._pause_timeout,
                self.pause_timeout_minutes * 60
            )
            return True
        return False

    def _pause_timeout(self, kwargs):
        """Called when door has been open for too long during a pause."""
        current_state = self.get_state(self.state_entity)

        if current_state == "Paused":
            self.log(f"Pause timeout - assuming emptied", level="INFO")
            self._transition_to_off("Door open too long during pause")

    def _running_watchdog_timeout(self, kwargs):
        """Safety watchdog - cycle running too long indicates sensor issue."""
        current_state = self.get_state(self.state_entity)
        if current_state == "Running":
            run_hours = self._get_run_duration_minutes() / 60
            self.log(f"WATCHDOG: Running for {run_hours:.1f}h exceeds max {self.max_running_hours}h - forcing Off", level="WARNING")
            self._transition_to_off(f"Watchdog: ran {run_hours:.1f}h (max {self.max_running_hours}h)")
        self.running_watchdog_timer = None

    def _unemptied_watchdog_timeout(self, kwargs):
        """Safety watchdog - Unemptied too long, user may have emptied without door sensor detecting."""
        current_state = self.get_state(self.state_entity)
        if current_state == "Unemptied":
            self.log(f"WATCHDOG: Unemptied for {self.unemptied_timeout_hours}h - assuming emptied", level="WARNING")
            self._transition_to_off(f"Watchdog: unemptied timeout ({self.unemptied_timeout_hours}h)")
        self.unemptied_watchdog_timer = None

    def _emptied_watchdog_timeout(self, kwargs):
        """Safety watchdog - Emptied too long, the door-close that should have ended the cycle
        was never seen (missed Zigbee edge, or the user left the door open to air the drum).

        Cleared before the transition so _reset_cycle_tracking does not try to cancel the very
        handle that is firing. force=True because a backstop that its own cooling period can
        swallow is not a backstop: this is the last line of defence for a state whose only other
        exit is a door event that, by the time we get here, demonstrably did not arrive."""
        self.emptied_watchdog_timer = None
        current_state = self.get_state(self.state_entity)
        if current_state == "Emptied":
            self.log(
                f"WATCHDOG: Emptied for {self.emptied_timeout_minutes:.0f} min - assuming the "
                f"door-close was missed, forcing Off",
                level="WARNING",
            )
            self._transition_to_off(
                f"Watchdog: emptied timeout ({self.emptied_timeout_minutes:.0f} min)", force=True
            )

    def _transition_to_running_from_pause(self, force=False):
        """Resume Running state after pause.

        Returns True/False from _should_change_state - same silent-refusal shape as
        _transition_to_unemptied (see its docstring for why that return value matters there);
        no caller here currently needs it.
        """
        if self._should_change_state("Running", force=force):
            self.state = "Running"
            self._set_state_entity( state="Running")
            self.door_opened_time = None
            self.log("State -> Running (resumed after pause)", level="INFO")

            if not self.poll_timer:
                self.poll_timer = self.run_in(self._poll_power, 60)
            return True
        return False

    def _evaluate_pause_exit(self, force=False):
        """Determine whether to go to Unemptied or Off when exiting Paused state."""
        if self._is_valid_completed_cycle():
            run_minutes_wall = self._get_run_duration_minutes()
            run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Pause-exit ")
            idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
            energy_kwh = self._get_energy_used()
            prog = self._classify_programme()
            became_unemptied = self._transition_to_unemptied(skip_announce=False, run_minutes=run_minutes, energy_used=energy_kwh, force=force)
            if became_unemptied:
                confirmed, is_human = self._get_confirmed_from_selector(prog)
                self._save_cycle_feedback(
                    predicted=prog,
                    confirmed=confirmed,
                    duration_min=run_minutes,
                    energy_kwh=energy_kwh,
                    max_power_w=self.max_power_w,
                    programme_confirmed_by_human=is_human,
                    duration_source=duration_source,
                    end_reason="low_power_detected",
                    idle_min=idle_min,
                )
        else:
            self._transition_to_off("Cycle interrupted or incomplete", force=force)

    def _reset_programme_selectors_to_unconfirmed(self):
        """Set programme / dryness / Skåne+ / time to idle defaults (user must confirm next cycle)."""
        if not self.reset_programme_on_idle:
            return
        try:
            if self.programme_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.programme_entity,
                    option=self.programme_unconfirmed_option,
                )
            if self.dryness_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.dryness_entity,
                    option=self.dryness_unselected_option,
                )
            if self.skane_plus_entity:
                self.call_service("input_boolean/turn_off", entity_id=self.skane_plus_entity)
            if self.reset_time_minutes_on_idle and self.time_minutes_entity:
                self.call_service(
                    "input_select/select_option",
                    entity_id=self.time_minutes_entity,
                    option=self.time_minutes_idle_default,
                )
            self.log("Programme selectors reset to unconfirmed / defaults for next cycle", level="DEBUG")
        except Exception as e:
            self.log(f"Could not reset programme selectors: {e}", level="WARNING")

    def _transition_to_off(self, reason, force=False):
        """Transition to Off state.

        Returns True/False from _should_change_state - same silent-refusal shape as
        _transition_to_unemptied (see its docstring for why that return value matters there);
        no caller here currently needs it.
        """
        if self._should_change_state("Off", force=force):
            self.state = "Off"
            self._set_state_entity( state="Off")
            self.log(f"State -> Off ({reason})", level="INFO")
            self._reset_programme_selectors_to_unconfirmed()
            self._reset_cycle_tracking()
            return True
        return False

    def _transition_to_unemptied(self, skip_announce=False, run_minutes=None, energy_used=None, force=False):
        """Transition to Unemptied state (cycle done, door still closed, waiting for user).

        skip_announce: If True, do not send the "dryer ready to empty" notification.
        run_minutes, energy_used: Optional corrected values (from history correction).

        Returns True when the transition actually happened, False when _should_change_state
        refused it (already Unemptied, or still inside cooling_period). Every caller that saves
        a _save_cycle_feedback record right after calling this MUST gate that save on the return
        value - the low-power finish path (_confirm_finished) re-polls roughly every stop_for/60s
        while watts stays low, and without the gate it kept re-entering here (refused, since
        cooling_period had not yet elapsed) while still unconditionally saving a fresh feedback
        record on every refused re-entry: nine junk finish_uld records in two bursts on
        2026-08-12, one per ~60s poll, each with a longer duration_min than the last.
        """
        if self._should_change_state("Unemptied", force=force):
            run_minutes = run_minutes if run_minutes is not None else self._get_run_duration_minutes()
            energy_used = energy_used if energy_used is not None else self._get_energy_used()

            # Stop periodic Running attribute updates and pending finish timers so nothing repaints progress.
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None
            self._safe_cancel_timer(self.low_power_timer)
            self.low_power_timer = None
            self._safe_cancel_timer(self.pattern_check_timer)
            self.pattern_check_timer = None

            self.state = "Unemptied"
            effective_key = self._get_confirmed_programme_key() or self.detected_programme
            attributes = {
                **self._attrs_clear_running_progress(),
                "cycle_complete": True,
                "run_time_minutes": round(run_minutes, 1),
                "derived_programme_key": effective_key,
            }
            if energy_used > 0:
                attributes["energy_used"] = round(energy_used, 3)
            if self.keep_fresh_detected:
                attributes["keep_fresh_detected"] = True

            self._set_state_entity( state="Unemptied", attributes=attributes, replace=True)

            if self.poll_timer:
                self._safe_cancel_timer(self.poll_timer)
                self.poll_timer = None

            # Cancel running watchdog, start unemptied watchdog
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = self.run_in(
                self._unemptied_watchdog_timeout,
                int(self.unemptied_timeout_hours * 3600)
            )

            self.log(f"State -> Unemptied (ran {run_minutes:.1f} min)", level="INFO")

            # Send notification - dryer done, please empty!
            # Skip when skip_announce=True (user opened door before we detected finish).
            announce_enabled = True
            if self.announce_entity:
                try:
                    announce_enabled = self.get_state(self.announce_entity) == "on"
                except Exception:
                    pass
            if (not skip_announce and self.sonos_notifier and not self.notification_sent
                    and announce_enabled):
                try:
                    msg = self.announce_message if getattr(self, "announce_message", None) else "Dryer is ready to be emptied"
                    self.sonos_notifier.notify(message=msg)
                    self.log("Dryer announcement sent", level="INFO")
                    self.notification_sent = True
                except Exception as e:
                    self.log(f"Error sending notification: {e}", level="ERROR")

            return True
        return False

    def _handle_force_emptied(self, event_name, data, kwargs):
        """dryer_force_emptied (dashboard Emptied button): the drum is empty but the door
        contact never saw the emptying. Only honored from Unemptied - any earlier state may
        still be a live cycle."""
        data = data or {}
        if self.state != "Unemptied":
            self.log(
                f"Force Emptied ignored from state {self.state!r} (only valid from Unemptied)",
                level="WARNING",
            )
            return
        reason = data.get("reason") or "Forced via event"
        self.log(f"Force Emptied via event ({reason})", level="INFO")
        self._transition_to_emptied(f"Forced emptied ({reason})")

    def _transition_to_emptied(self, reason):
        """Transition to Emptied state (door open, user is emptying).

        Returns True/False from _should_change_state - same silent-refusal shape as
        _transition_to_unemptied (see its docstring for why that return value matters there);
        no caller here currently needs it.
        """
        if self._should_change_state("Emptied", force=True):  # Door event bypasses cooling
            energy_used = self._get_energy_used()
            run_minutes = self._get_run_duration_minutes()

            self.state = "Emptied"
            effective_key = self._get_confirmed_programme_key() or self.detected_programme
            attributes = {
                **self._attrs_clear_running_progress(),
                "reason": reason,
                "run_time_minutes": round(run_minutes, 1) if run_minutes > 0 else None,
                "derived_programme_key": effective_key,
            }
            if energy_used > 0:
                attributes["energy_used"] = round(energy_used, 3)
            if self.keep_fresh_detected:
                attributes["keep_fresh_detected"] = True

            self._set_state_entity( state="Emptied", attributes=attributes, replace=True)

            # Cancel unemptied watchdog since we're now emptying
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None

            # Arm the emptied watchdog: from here the only exit is the door closing, so this is
            # the sole backstop if that edge is missed. Cancelled in _reset_cycle_tracking once
            # a transition to Off actually lands.
            self._safe_cancel_timer(self.emptied_watchdog_timer)
            self.emptied_watchdog_timer = self.run_in(
                self._emptied_watchdog_timeout,
                int(self.emptied_timeout_minutes * 60),
            )

            # Re-enable announce toggle for next cycle (match washer)
            if self.announce_entity:
                try:
                    self.call_service("input_boolean/turn_on", entity_id=self.announce_entity)
                    self.log("Announce toggle reset to on for next cycle", level="DEBUG")
                except Exception as e:
                    self.log(f"Could not reset announce toggle: {e}", level="WARNING")

            self.log(f"State -> Emptied ({reason})", level="INFO")
            return True
        return False

    def _reset_cycle_tracking(self):
        """Reset all cycle-related tracking variables."""
        self.start_time = None
        self.energy_start = None
        self.door_opened_time = None
        self.door_opened_during_cycle = False
        self.keep_fresh_detected = False
        self.power_readings = []
        self.high_power_counter = 0

        if self.poll_timer:
            self._safe_cancel_timer(self.poll_timer)
            self.poll_timer = None

        if self.pause_timer:
            self._safe_cancel_timer(self.pause_timer)
            self.pause_timer = None

        if self.low_power_timer:
            self._safe_cancel_timer(self.low_power_timer)
            self.low_power_timer = None

        if self.pattern_check_timer:
            self._safe_cancel_timer(self.pattern_check_timer)
            self.pattern_check_timer = None
        if self.classify_timer:
            self._safe_cancel_timer(self.classify_timer)
            self.classify_timer = None

        self.expected_dur_at_start = None
        self.detected_programme = "unknown"
        self.max_power_w = 0.0

        # Cancel watchdog timers
        if self.running_watchdog_timer:
            self._safe_cancel_timer(self.running_watchdog_timer)
            self.running_watchdog_timer = None
        if self.unemptied_watchdog_timer:
            self._safe_cancel_timer(self.unemptied_watchdog_timer)
            self.unemptied_watchdog_timer = None
        # Cancelled here rather than at the call site, so it is only ever dropped once a
        # transition to Off has actually landed. Cancelling a backstop BEFORE attempting the
        # transition it backs is the exact shape of the 34h Paused wedge (see _handle_door_closed).
        if self.emptied_watchdog_timer:
            self._safe_cancel_timer(self.emptied_watchdog_timer)
            self.emptied_watchdog_timer = None

        if self.start_confirm_timer:
            self._safe_cancel_timer(self.start_confirm_timer)
            self.start_confirm_timer = None
        self.door_fast_start_armed_until = None

    def _power_changed(self, entity, attr, old, new, kwargs):
        try:
            if new in ["unknown", "unavailable"]:
                self._handle_unavailable(entity, attr, old, new, kwargs)
                return
            watts = float(new or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(entity, attr, old, new, kwargs)
            return

        # Plug is reporting numbers again - stand down the dead-plug watchdog and the
        # pending forced-Off grace.
        if self._plug_outage_push_timer:
            self._safe_cancel_timer(self._plug_outage_push_timer)
            self._plug_outage_push_timer = None
        if self._plug_outage_pushed:
            self._plug_outage_pushed = False
            self._push_mobile("Power plug is reporting again - dryer monitoring resumed.")
        self._cancel_power_unavailable_grace()

        current_state = self.get_state(self.state_entity)

        # Record power readings when running
        if current_state == "Running":
            self._record_power_reading(watts)
            if watts > self.max_power_w:
                self.max_power_w = watts

        # Do NOT revert Unemptied -> Running when power rises again. After a real cycle ends, Miele
        # keep-fresh / anti-crease often draws >= start_w while the door is still closed; that is
        # still Unemptied (waiting to empty), not a "false" Unemptied. Leave Unemptied until door
        # or unemptied watchdog.

        # Track sustained high power
        if watts >= self.main_cycle_power:
            self.high_power_counter += 1
        else:
            self.high_power_counter = 0

        # High power branch (start detection) - single pending confirm timer (no stacking)
        if watts >= self.start_w:
            if current_state == "Off":
                if not self.start_confirm_timer:
                    now = self._now_utc()
                    armed = (
                        self.door_fast_start_armed_until is not None
                        and now <= self.door_fast_start_armed_until
                    )
                    delay = self.door_close_fast_confirm_s if armed else self.run_for
                    self.log(
                        f"Power >= {self.start_w}W -> scheduling start confirm in {delay}s "
                        f"({'fast-start window' if armed else 'normal'})",
                        level="DEBUG",
                    )
                    self.start_confirm_timer = self.run_in(self._confirm_running, delay)
        elif current_state == "Off":
            if self.start_confirm_timer:
                self._safe_cancel_timer(self.start_confirm_timer)
                self.start_confirm_timer = None

        # Low power branch (finish detection)
        elif current_state == "Running" and watts <= self.stop_w:
            if not self.low_power_timer:
                self.log(f"Power <= {self.stop_w}W -> may be finished", level="DEBUG")

                # Start pattern detection for keep-fresh/anti-crease
                if not self.pattern_check_timer and not self.keep_fresh_detected:
                    self.pattern_check_timer = self.run_every(
                        self._check_power_pattern,
                        self.datetime() + timedelta(seconds=self.power_reading_interval),
                        self.power_reading_interval,
                    )

                self.low_power_timer = self.run_in(self._confirm_finished, self.stop_for)

        # Recovered spike
        elif current_state == "Running" and watts > self.stop_w and self.low_power_timer:
            self._safe_cancel_timer(self.low_power_timer)
            self.low_power_timer = None
            self.log(f"Power recovered to {watts}W", level="DEBUG")

    def _check_power_pattern(self, kwargs):
        """Check power readings for anti-crease/keep-fresh pattern (Miele TCB150 WP feature)."""
        if len(self.power_readings) < self.pattern_window or self.state != "Running":
            return

        try:
            mean_power = statistics.mean(self.power_readings)
            max_power = max(self.power_readings)
            min_power = min(self.power_readings)
            stdev = statistics.stdev(self.power_readings) if len(self.power_readings) > 1 else 0

            # Typical keep-fresh/anti-crease pattern: intermittent tumbles with zero periods
            has_zero_periods = min_power < 5
            has_moderate_spikes = 100 < max_power < self.main_cycle_power
            not_main_cycle = mean_power < self.main_cycle_power / 3

            main_cycle_done = False

            if has_zero_periods and has_moderate_spikes and not_main_cycle:
                main_cycle_done = True

            if mean_power < 250 and max_power < self.main_cycle_power and stdev > 50:
                main_cycle_done = True

            if main_cycle_done:
                self.log("Anti-crease/keep-fresh mode detected - dryer main cycle complete", level="INFO")
                self.keep_fresh_detected = True
                run_min = self._get_run_duration_minutes()
                prog = self._classify_programme()
                effective_key = self._get_confirmed_programme_key() or prog
                guard_dur = self._get_guard_duration(tick_prog=effective_key)
                if run_min < guard_dur * 0.8:
                    self.log(f"Keep-fresh: run {run_min:.0f}min < 80% of {guard_dur:.0f}min - blocking Unemptied", level="DEBUG")
                elif not self._is_valid_completed_cycle():
                    self.log("Keep-fresh: cycle validation failed", level="DEBUG")
                else:
                    profile = self._get_profile(prog)
                    if profile.get("is_real", True) is False:
                        self.log("Keep-fresh: non-real programme (e.g. finish_uld) - not declaring Unemptied", level="DEBUG")
                    else:
                        run_minutes_wall = run_min
                        run_minutes, duration_source = self._correct_duration(run_minutes_wall, log_prefix="Keep-fresh ")
                        idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                        energy_kwh = self._get_energy_used()
                        became_unemptied = self._transition_to_unemptied(skip_announce=False, run_minutes=run_minutes, energy_used=energy_kwh)
                        if became_unemptied:
                            confirmed, is_human = self._get_confirmed_from_selector(prog)
                            self._save_cycle_feedback(
                                predicted=prog,
                                confirmed=confirmed,
                                duration_min=run_minutes,
                                energy_kwh=energy_kwh,
                                max_power_w=self.max_power_w,
                                programme_confirmed_by_human=is_human,
                                duration_source=duration_source,
                                end_reason="keep_fresh_detected",
                                idle_min=idle_min,
                            )
                if self.pattern_check_timer:
                    self._safe_cancel_timer(self.pattern_check_timer)
                    self.pattern_check_timer = None

        except Exception as e:
            self.log(f"Error in pattern analysis: {e}", level="WARNING")

    def _poll_power(self, kwargs):
        """Conditional polling"""
        current_state = self.get_state(self.state_entity)

        if current_state not in ("Running", "Paused"):
            if self.poll_timer:
                self._safe_cancel_timer(self.poll_timer)
                self.poll_timer = None
            return

        if current_state == "Paused" and self.get_state(self.door_sensor) == "off":
            self.log("Poll: state Paused but door is closed - applying pause-exit handling", level="INFO")
            # This timer has already fired, so drop the dead handle before transitioning:
            # _transition_to_running_from_pause only re-arms polling when poll_timer is None,
            # and the Off/Unemptied paths cancel it themselves via _reset_cycle_tracking.
            self.poll_timer = None
            if self._get_current_power() >= self.start_w:
                self._transition_to_running_from_pause(force=True)
            else:
                self._evaluate_pause_exit(force=True)
            return

        current_power_state = self.get_state(self.power_sensor)
        if current_power_state in ["unknown", "unavailable"]:
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        try:
            watts = float(current_power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, current_power_state, {})
            return

        self._power_changed(self.power_sensor, None, None, watts, {})
        self.poll_timer = self.run_in(self._poll_power, 60)

    def _confirm_running(self, kwargs):
        self.start_confirm_timer = None

        power_state = self.get_state(self.power_sensor)
        if power_state in ["unknown", "unavailable", None]:
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        try:
            watts_confirm = float(power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        if watts_confirm < self.start_w:
            self.log(
                f"Start confirm skipped: live power {watts_confirm:.1f}W < {self.start_w}W (stale/noise) - "
                f"fast-start arm preserved if still in window",
                level="DEBUG",
            )
            return

        if watts_confirm >= self.start_w:
            if self._should_change_state("Running"):
                self.state = "Running"
                self.start_time = self._now_utc()
                self.cycle_id = uuid.uuid4().hex  # fresh identity for a genuinely new cycle
                self._feedback_saved_cycle_id = None  # new cycle - clear the duplicate-save guard
                self.keep_fresh_detected = False
                self.power_readings = []
                self.notification_sent = False
                self.door_opened_during_cycle = False
                self.detected_programme = "unknown"
                self.max_power_w = watts_confirm

                try:
                    energy = self.get_state(self.energy_sensor)
                    if energy is not None and energy not in ["unknown", "unavailable"]:
                        self.energy_start = float(energy)
                except (ValueError, TypeError):
                    self.energy_start = None

                prog = self._classify_programme()
                self.detected_programme = prog
                effective_key = self._get_confirmed_programme_key() or prog
                self.expected_dur_at_start = self._get_guard_duration(tick_prog=effective_key)
                self._set_state_entity( state="Running")
                self._update_running_attributes()

                if not self.poll_timer:
                    self.poll_timer = self.run_in(self._poll_power, 60)
                if not self.classify_timer:
                    self.classify_timer = self.run_in(self._tick_classify, 30)

                # Start running watchdog
                self._safe_cancel_timer(self.running_watchdog_timer)
                self.running_watchdog_timer = self.run_in(
                    self._running_watchdog_timeout,
                    int(self.max_running_hours * 3600)
                )

                self.log("State -> Running", level="INFO")
                self.door_fast_start_armed_until = None

    def _confirm_finished(self, kwargs):
        """Confirm the cycle has finished - power dropped, door still closed. 80% guard."""
        current_state = self.get_state(self.state_entity)
        if current_state != "Running":
            self.low_power_timer = None
            return

        power_state = self.get_state(self.power_sensor)
        if power_state in ["unknown", "unavailable", None]:
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        try:
            watts = float(power_state or 0)
        except (ValueError, TypeError):
            self._handle_unavailable(self.power_sensor, None, None, power_state, {})
            return

        if self.keep_fresh_detected or watts <= self.stop_w:
            run_min = self._get_run_duration_minutes()
            prog = self._classify_programme()
            effective_key = self._get_confirmed_programme_key() or prog
            guard_dur = self._get_guard_duration(tick_prog=effective_key)
            if run_min < guard_dur * 0.8:
                self.log(f"Power-based: run {run_min:.0f}min < 80% of {guard_dur:.0f}min - blocking", level="DEBUG")
                self.low_power_timer = None
                return
            if self._is_valid_completed_cycle():
                profile = self._get_profile(prog)
                skip_announce = profile.get("is_real", True) is False  # e.g. finish_uld
                run_minutes_wall = run_min
                run_minutes, duration_source = self._correct_duration(run_minutes_wall)
                idle_min = run_minutes_wall - run_minutes if duration_source and run_minutes_wall > run_minutes else None
                energy_kwh = self._get_energy_used()
                became_unemptied = self._transition_to_unemptied(skip_announce=skip_announce, run_minutes=run_minutes, energy_used=energy_kwh)
                if became_unemptied:
                    # Gated on the transition actually landing: _should_change_state refuses a
                    # repeat call once already Unemptied or still inside cooling_period, and this
                    # low-power path is re-entered roughly every stop_for/60s for as long as watts
                    # stays <= stop_w (finish_uld is_real: False, so skip_announce hides the
                    # repetition from notifications entirely). Before this gate, every refused
                    # re-entry still saved a fresh feedback record regardless: nine junk finish_uld
                    # records in two bursts on 2026-08-12, one per ~60s poll, duration_min growing
                    # each time - see _transition_to_unemptied's docstring.
                    confirmed, is_human = self._get_confirmed_from_selector(prog)
                    self._save_cycle_feedback(
                        predicted=prog,
                        confirmed=confirmed,
                        duration_min=run_minutes,
                        energy_kwh=energy_kwh,
                        max_power_w=self.max_power_w,
                        programme_confirmed_by_human=is_human,
                        duration_source=duration_source,
                        end_reason="low_power_detected",
                        idle_min=idle_min,
                    )
                if self.pattern_check_timer:
                    self._safe_cancel_timer(self.pattern_check_timer)
                    self.pattern_check_timer = None
            else:
                self.log("Cycle incomplete - waiting", level="DEBUG")

        self.low_power_timer = None

    def _handle_unavailable(self, entity, attribute, old, new, kwargs):
        """Handle entity becoming unavailable or unknown."""
        if entity == self.state_entity:
            # The dryer's own state entity dropping out (HA restart, recorder hiccup) is a
            # UI-visibility problem, not a cycle-tracking one - never wipe cycle state for it
            # (dishwasher_monitor.py ~2242-2246 is the same policy for its own state entity).
            self.log(
                f"{entity} became {new!r} - leaving cycle state untouched, UI may be stale until it is back",
                level="WARNING",
            )
            return
        if entity == self.power_sensor:
            self._begin_plug_outage_grace()
        self._begin_power_unavailable_off_grace(new)

    def _begin_power_unavailable_off_grace(self, new_label):
        """Tolerate a short power-sensor dropout (HA restart, ESPHome OTA flash - these
        routinely blip the plug for well under the grace) before wiping an in-progress cycle;
        see washer_monitor.py's power_unavailable_off_after_seconds (same guard, proven there
        since 2026-07-17 - an instant force-Off used to destroy cycle tracking/learning data on
        every plug blip). Only a sustained outage past the grace runs the old wipe."""
        if self.power_unavailable_off_timer and self.timer_running(self.power_unavailable_off_timer):
            return
        self.power_unavailable_off_timer = self.run_in(
            self._power_unavailable_off_timeout,
            self.power_unavailable_off_after_seconds,
        )
        self.log(
            f"{self.power_sensor} {new_label!r}; waiting {self.power_unavailable_off_after_seconds}s before "
            f"forcing Off (short dropouts - HA restart / plug OTA - are ignored)",
            level="WARNING",
        )

    def _cancel_power_unavailable_grace(self):
        """Power reading is valid again; cancel the pending forced-Off transition."""
        self._safe_cancel_timer(self.power_unavailable_off_timer)
        self.power_unavailable_off_timer = None

    def _power_unavailable_off_timeout(self, kwargs):
        self.power_unavailable_off_timer = None
        if self.get_state(self.power_sensor) not in ("unknown", "unavailable", None):
            self.log(
                f"Power sensor recovered before the {self.power_unavailable_off_after_seconds}s grace expired",
                level="INFO",
            )
            return
        self.log(
            f"{self.power_sensor} unavailable >= {self.power_unavailable_off_after_seconds}s - setting state to Off",
            level="WARNING",
        )
        self.state = "Off"
        # UTC to match _should_change_state's comparison - a naive stamp here would make the
        # next cooling check raise TypeError on mixed naive/aware operands.
        self.last_state_change = self._now_utc()
        self._set_state_entity( state="Off")
        self._reset_programme_selectors_to_unconfirmed()
        self._reset_cycle_tracking()

    def _begin_plug_outage_grace(self):
        """Short plug dropouts are routine; only a lasting outage pages the phone."""
        if self._plug_outage_pushed:
            return
        if self._plug_outage_push_timer and self.timer_running(self._plug_outage_push_timer):
            return
        self._plug_outage_push_timer = self.run_in(
            self._plug_outage_push_timeout, self.plug_outage_push_after_seconds
        )

    def _plug_outage_push_timeout(self, kwargs):
        self._plug_outage_push_timer = None
        if self.get_state(self.power_sensor) not in ("unknown", "unavailable", None):
            return
        self._plug_outage_pushed = True
        self._push_mobile(
            f"Power plug stopped reporting (unavailable >= {self.plug_outage_push_after_seconds}s) - "
            f"cycle monitoring is blind and the dryer just looks Off. Check the plug/WiFi."
        )

    def _push_mobile(self, message):
        """Page the phone (plug outage / recovery) - same pattern as gw2000a_watchdog."""
        try:
            notifier = self.get_app("MobileNotifier")
            if notifier is None:
                self.log("MobileNotifier app not found - cannot push", level="WARNING")
                return
            self.create_task(notifier.notify(title="Dryer", message=message, target=self.notify_target))
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")
