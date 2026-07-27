"""
ActorAttribution - answers "who was home at time T, if exactly one person was" so
appliance monitors (washer/dishwasher/dryer) can record who started or emptied a load.
Physical button presses carry no HA context_user_id - only app-issued service calls do -
so sole-occupancy is the only free signal available today.

NEVER GUESS. attribute() only ever names a person when the tracked home-set has held
exactly one person, continuously and fully observable, for the whole stability_seconds
window ending at the anchor, AND the apartment door has not opened in the trailing
door_settle_seconds (a person who just walked in produces no presence *change* to be
unstable about - their phone hasn't reported home yet - so a fresh door-open is read as
"someone could have entered unnoticed", never as confirmation). Every other case returns
unknown with a specific machine-readable reason. A wrong attribution silently poisons an
appliance's per-person history/learning forever; a missing one just leaves a blank -
recoverable the next time the signal is clean. See _by_sole_occupancy for the full
seven-rule table, evaluated in order, each one a short-circuit.

Known residual (not closed by this app): sole-occupancy is a WEAK signal by construction.
An untracked human physically inside the flat - a guest, a cleaner, a kid's friend, or
even one of the three tracked people without their phone/location switched on - is
invisible to person.* state entirely, and will read as either "nobody home" or get folded
into whoever else happens to be tracked as home. A "sole occupant" verdict can still be
wrong if such a person pressed the actual button.

Phase 2 seam: attribute() is a thin dispatcher today, `return self._by_sole_occupancy(...)`,
specifically so a future BLE-presence signal can be slotted in ahead of it without
touching any call site:
    return self._by_ble(event, anchor) or self._by_sole_occupancy(event, anchor, evaluated_at)
Appliance monitors only ever call attribute()/probe() - nothing else in this app's public
surface needs to change shape for that to work.

Hard rule - zero AppDaemon API calls inside attribute() and everything it calls (no
get_state/call_service/run_in/create_task/set_state/get_history): it reads only the
in-memory transition log this app's listeners/tick/backfill maintain elsewhere in this
file. This is what makes it safe to call from a synchronous worker-thread callback (e.g.
an appliance monitor's own listen_state handler) or from an async context without
awaiting - the opposite ordering (an AppDaemon sync-style API call left un-awaited on the
event loop) silently returns a live coroutine that is simply never run, so the intended
action just never happens. attribute() is also guaranteed to never raise: every entry
point is wrapped so any internal error (corrupt state, a config value of the wrong type,
even this app never having initialized at all) falls back to a well-formed unknown dict
instead of propagating.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import collections
import time
from datetime import datetime, timedelta, timezone


def _parse_utc(s):
    """Parse ISO timestamp to timezone-aware UTC datetime. Handles 'Z' and no suffix.
    Same idiom as washer_monitor.py's module-level _parse_utc (duplicated locally -
    this repo's convention is per-app copies of small history helpers, not a shared
    import, e.g. dryer_monitor.py also carries its own copy)."""
    if not s:
        return None
    s = str(s).strip().replace("Z", "+00:00")
    if s.endswith("+00:00"):
        pass
    elif "+" not in s[-7:] and "-" not in s[-7:]:
        s = s + "+00:00"  # assume UTC if no offset
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


# A person entity is unobservable (never "away") whenever its state is one of these -
# see MobileNotifier.get_people_home() in mobile_notifier.py for the notification-side
# rule this deliberately does NOT copy: it treats any non-"not_home" value as home, which
# is right for "should we bother sending a push" and fatally wrong for attribution, where
# a zone name ("Work", "Gym") must count as NOT home rather than as home.
_UNOBSERVABLE_STATES = frozenset({None, "unknown", "unavailable"})

_VERSION = 1

# Sentinel distinguishing "never seen this entity at all" from "seen and its value
# happens to be None/some other falsy thing" when reconciling the tick's fresh read
# against the cached view.
_MISSING = object()

# One row in the presence transition log: the home-set and unobservable-set that were
# jointly in effect from `ts` until the next entry (or now, for the last one). Only
# appended when this PAIR changes from the previous entry - e.g. person.claudia moving
# between "not_home" and a zone name ("Gym") is still just "away" and does not grow the
# log. Kept as an immutable tuple of these and always replaced wholesale
# (self._log = self._log + (entry,)) - AppDaemon calls listen_state callbacks from
# worker threads, so a list another thread might be iterating must never be mutated.
_Snapshot = collections.namedtuple("_Snapshot", ("ts", "home", "unobservable"))

# sensor.household_actor state strings for the non-attributed verdicts - short single
# words in the same style as the attributed case (which is just the person's own key).
# door_event_recent/no_history aren't explicitly named in the app's spec's example list
# (mikkel/multi/nobody/unstable/unobservable); these two follow the same "one
# recognisable word" convention rather than the full snake_case reason.
_REASON_TO_STATE = {
    "multi_occupant": "multi",
    "nobody_home": "nobody",
    "presence_unstable": "unstable",
    "presence_unobservable": "unobservable",
    "door_event_recent": "door_recent",
    "no_history": "no_history",
}


class ActorAttribution(hass.Hass):
    # Class-level defaults so attribute()/probe() are well-defined even before
    # initialize() has run (e.g. a call that races app startup) or on a bare test
    # double - constraint: attribute() must never raise AttributeError chasing state
    # that initialize() hasn't set yet. Everything attribute() reads lives here.
    _log = ()                 # tuple[_Snapshot], ascending by ts, append-only
    _door_events = ()         # tuple[datetime]: door OPEN edges only, ascending
    _door_state = None        # last known raw door_entity state (None -> unobservable)
    stability_seconds = 600
    door_settle_seconds = 300
    retention_hours = 26

    def initialize(self):
        a = self.args.get
        self.person_entities = list(a("person_entities", [
            "person.mikkel", "person.kristine", "person.claudia",
        ]))
        self.door_entity = a("door_entity", "binary_sensor.apartment_door_open")
        self.stability_seconds = int(a("stability_seconds", 600))
        self.door_settle_seconds = int(a("door_settle_seconds", 300))
        self.retention_hours = float(a("retention_hours", 26))
        self.publish_sensor = a("publish_sensor", "sensor.household_actor")

        self._person_state = {}  # person_key -> last known raw HA state (tick/log cache)

        for entity in self.person_entities:
            self.listen_state(self._on_person_change, entity, person=self._person_key(entity))
        self.listen_state(self._on_door_change, self.door_entity)
        self.listen_event(self._on_probe_event, "actor_attribution_probe")

        # Deferred so initialize() returns immediately - get_history's response time
        # must never gate AppDaemon's startup. Until this callback runs, attribute()
        # correctly answers "no_history" for everything (rule 1) rather than guessing
        # from an empty log.
        self.run_in(self._backfill, 5)
        self.run_every(self._tick, "now+30", 300)

        self.log(
            f"ActorAttribution initialized: {len(self.person_entities)} person(s), "
            f"door={self.door_entity}, stability={self.stability_seconds}s, "
            f"door_settle={self.door_settle_seconds}s, retention={self.retention_hours:.0f}h",
            level="INFO",
        )

    # ------------------------------------------------------------------------------
    # PURE ZONE: attribute() and everything below down to _person_key perform ZERO
    # AppDaemon API calls (no get_state/call_service/run_in/create_task/set_state/
    # get_history) and read only the in-memory state the listeners/tick/backfill below
    # maintain. See module docstring for why this boundary matters.
    # ------------------------------------------------------------------------------

    def attribute(self, event: str, at=None) -> dict:
        try:
            return self._attribute_unsafe(event, at)
        except Exception as e:
            try:
                self.log(
                    f"ActorAttribution.attribute({event!r}) failed internally, "
                    f"returning unknown rather than guess: {e}",
                    level="ERROR",
                )
            except Exception:
                pass  # self.log itself may not exist yet (e.g. pre-initialize()) - never raise
            return self._safe_unknown()

    def probe(self) -> dict:
        """Current verdict (anchor=now) for diagnostics - see also the
        actor_attribution_probe listen_event hook below for an on-demand log line."""
        return self.attribute("probe", at=None)

    def _attribute_unsafe(self, event, at):
        evaluated_at = self._now_utc()
        anchor = self._coerce_utc(at) if at is not None else evaluated_at
        result = self._by_sole_occupancy(event, anchor, evaluated_at)
        if self._door_state in _UNOBSERVABLE_STATES:
            # The door guard itself is blind, so rule 4 below could have missed a real
            # open edge. Per spec we still attribute - the absence of a recorded edge is
            # not proof none happened, and refusing to attribute whenever the door
            # sensor blips would be its own (different) false-negative problem - but the
            # ledger should show the evidence was weaker than usual.
            result["reason_note"] = "door_guard_unavailable"
        return result

    def _by_sole_occupancy(self, event, anchor, evaluated_at):
        """The seven decision rules, evaluated in order, looking BACKWARD from anchor
        only. Phase 2: attribute() will try _by_ble() first and only fall back to this -
        see module docstring."""
        win_start = anchor - timedelta(seconds=self.stability_seconds)
        retention_cutoff = evaluated_at - timedelta(hours=self.retention_hours)

        # Rule 1: the anchor predates what this app even claims to retain, OR the log
        # hasn't accumulated stability_seconds of coverage back from it yet (fresh
        # deploy, backfill still pending/failed). Either way we simply don't know.
        if anchor < retention_cutoff:
            return self._unknown(anchor, evaluated_at, "no_history")
        window = self._snapshots_covering_window(win_start, anchor)
        if not window:
            return self._unknown(anchor, evaluated_at, "no_history")

        effective_home = window[-1].home  # state in effect AT anchor

        # Rule 2: any tracked person unobservable anywhere in the window forces unknown -
        # never silently folded into "away" (that is exactly what MobileNotifier's
        # get_people_home() does for notifications, and exactly what would make this
        # app guess).
        if any(entry.unobservable for entry in window):
            return self._unknown(anchor, evaluated_at, "presence_unobservable", effective_home)

        # Rule 3: the home-set itself must not have moved anywhere in the window, or a
        # "sole occupant" verdict could really mean "two different people, one after
        # another" rather than one person alone for the full stability_seconds.
        if len({entry.home for entry in window}) > 1:
            return self._unknown(anchor, evaluated_at, "presence_unstable", effective_home)

        # Rule 4: a fresh arrival produces no presence *change* to be unstable about -
        # their phone hasn't reported home yet - so a door-open in the trailing
        # door_settle_seconds is the only signal that someone could have walked in
        # unnoticed. Backward-looking only: an open AFTER anchor cannot explain who
        # pressed a button BEFORE anchor.
        door_win_start = anchor - timedelta(seconds=self.door_settle_seconds)
        if any(door_win_start <= ts <= anchor for ts in self._door_events):
            return self._unknown(anchor, evaluated_at, "door_event_recent", effective_home)

        # Rule 5: a physical press requires a body in the flat. "Nobody home" here means
        # the presence model is wrong (a phone left behind, a stale away-state, etc.),
        # never that the flat was actually empty - so this is only logged, never used to
        # fall back to "whoever we last saw home" (that would be exactly the guess this
        # app exists to avoid).
        if not effective_home:
            self.log(
                f"ActorAttribution: nobody_home verdict for event={event!r} at "
                f"{self._iso(anchor)} - a physical press implies presence, so treat this "
                f"as a presence-model gap, not as fact",
                level="INFO",
            )
            return self._unknown(anchor, evaluated_at, "nobody_home", effective_home)

        # Rule 6: more than one person home - no free signal says which of them pressed
        # the button.
        if len(effective_home) > 1:
            return self._unknown(anchor, evaluated_at, "multi_occupant", effective_home)

        # Rule 7: exactly one - the only case this app ever names a person.
        person = next(iter(effective_home))
        return {
            "person": person,
            "method": "sole_occupant",
            "reason": None,
            "people_home": sorted(effective_home),
            "anchor": self._iso(anchor),
            "evaluated_at": self._iso(evaluated_at),
            "version": _VERSION,
        }

    def _snapshots_covering_window(self, win_start, win_end):
        """Every self._log entry whose validity overlaps [win_start, win_end]: the entry
        already in effect AT win_start (the "carry-in"), if any, plus every later entry
        that starts at-or-before win_end. self._log is append-only and sorted ascending
        by ts, so a single forward pass suffices.

        Returns [] whenever there is no carry-in (no entry at or before win_start) - even
        if some entries exist strictly inside the window - because that means we have a
        blind spot for the start of the window and cannot rule out instability or
        unobservability we simply have no record of. That blind spot is exactly what
        rule 1 ("no_history") exists to catch."""
        carry = None
        within = []
        for entry in self._log:
            if entry.ts <= win_start:
                carry = entry
            elif entry.ts <= win_end:
                within.append(entry)
            else:
                break
        if carry is None:
            return []
        return [carry] + within

    def _unknown(self, anchor, evaluated_at, reason, people_home=frozenset()):
        return {
            "person": None,
            "method": "unknown",
            "reason": reason,
            "people_home": sorted(people_home),
            "anchor": self._iso(anchor),
            "evaluated_at": self._iso(evaluated_at),
            "version": _VERSION,
        }

    @staticmethod
    def _safe_unknown():
        """Absolute last resort - attribute() must NEVER raise, even on a bare
        __new__() instance where self.log doesn't exist at all (see tests). No
        attribute access beyond what any freshly-constructed object already has; no
        AppDaemon calls."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {
            "person": None,
            "method": "unknown",
            "reason": "no_history",
            "people_home": [],
            "anchor": now,
            "evaluated_at": now,
            "version": _VERSION,
        }

    @staticmethod
    def _coerce_utc(dt):
        """Accept a naive datetime as UTC - same assumption _parse_utc makes elsewhere
        in this repo - rather than raising on tzinfo-less input."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _iso(dt):
        """UTC ISO8601 with 'Z' suffix - matches washer_monitor.py's _format_utc
        convention. "" for None (never used for anchor/evaluated_at, which are always
        real datetimes; used for the optional stable_since/last_door_open sensor
        attributes, where AppDaemon's set_state silently drops None/False/0 but "" is
        never dropped - see smart_cooling.py's _publish() for the mechanism)."""
        if dt is None:
            return ""
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _person_key(entity_id):
        """person.mikkel -> mikkel. Mirrors MobileNotifier.get_people_home()'s own
        derivation (mobile_notifier.py) so the value doubles as a notify target with
        zero translation needed by callers."""
        return entity_id.replace("person.", "").lower()

    # ------------------------------------------------------------------------------
    # Listeners, periodic reconciliation, history backfill, publish: everything below
    # is allowed (and expected) to call AppDaemon APIs.
    # ------------------------------------------------------------------------------

    def _on_person_change(self, entity, attribute, old, new, kwargs):
        try:
            person = kwargs.get("person") or self._person_key(entity)
            self._person_state[person] = new
            self._append_snapshot_if_changed(self._now_utc())
            self._publish()
        except Exception as e:
            self.log(f"ActorAttribution: person-change handler failed for {entity}: {e}", level="ERROR")

    def _on_door_change(self, entity, attribute, old, new, kwargs):
        try:
            self._door_state = new
            if new == "on" and old != "on":
                self._door_events = self._door_events + (self._now_utc(),)
                self._prune_door_events()
            self._publish()
        except Exception as e:
            self.log(f"ActorAttribution: door-change handler failed: {e}", level="ERROR")

    def _on_probe_event(self, event_name, data, kwargs):
        """Test hook: fire event actor_attribution_probe (e.g. from Developer Tools ->
        Events) to log the current verdict at INFO without waiting for a real appliance
        cycle. Mirrors washer_monitor.py's washer_test_confirm_push hook
        (_on_test_confirm_push)."""
        try:
            self.log(f"ActorAttribution probe: {self.probe()}", level="INFO")
        except Exception as e:
            self.log(f"ActorAttribution probe event handler failed: {e}", level="ERROR")

    def _tick(self, kwargs):
        """Re-read every tracked entity and reconcile against the cached view. AppDaemon
        4.5.13 has a documented failure mode where get_state can keep serving a stale
        value for minutes even though the matching listen_state callback already fired
        (a dropped store update) - this is the safety net for exactly that: any
        disagreement is applied as a fresh transition stamped now, which resets the
        stability clock, and logged at WARNING. Fail-safe direction: a stale-store blip
        costs recall (attribute() answers unknown for one more window), never precision
        (it can never make attribute() name someone it shouldn't)."""
        try:
            now = self._now_utc()
            for entity in self.person_entities:
                person = self._person_key(entity)
                try:
                    fresh = self.get_state(entity)
                except Exception as e:
                    self.log(f"ActorAttribution tick: get_state({entity}) failed: {e}", level="WARNING")
                    continue
                cached = self._person_state.get(person, _MISSING)
                if fresh != cached:
                    self.log(
                        f"ActorAttribution: {entity} disagreed with the cached view at "
                        f"reconcile ({cached!r} -> {fresh!r}) - applying as a fresh "
                        f"transition now instead of trusting the stale cache",
                        level="WARNING",
                    )
                    self._person_state[person] = fresh
            self._append_snapshot_if_changed(now)

            try:
                door_fresh = self.get_state(self.door_entity)
            except Exception as e:
                door_fresh = _MISSING
                self.log(f"ActorAttribution tick: get_state({self.door_entity}) failed: {e}", level="WARNING")
            if door_fresh is not _MISSING and door_fresh != self._door_state:
                self.log(
                    f"ActorAttribution: {self.door_entity} disagreed with the cached "
                    f"view at reconcile ({self._door_state!r} -> {door_fresh!r})",
                    level="WARNING",
                )
                was_on = self._door_state == "on"
                self._door_state = door_fresh
                if door_fresh == "on" and not was_on:
                    self._door_events = self._door_events + (now,)

            self._prune_log(now)
            self._prune_door_events(now)
            self._publish()
        except Exception as e:
            self.log(f"ActorAttribution tick failed: {e}", level="ERROR")

    def _backfill(self, kwargs):
        """Seed self._log/_door_events/_door_state from HA history so attribute() has
        real coverage as soon as live traffic starts, instead of needing
        retention_hours of fresh uptime before rule 1 ("no_history") stops firing for
        every call. Runs from the run_in(..., 5) callback armed in initialize() so
        AppDaemon's blocking startup sequence is never held hostage by however long
        get_history takes to answer. Any entity whose history can't be fetched (or comes
        back empty) is left classified unobservable from the start - never guessed - per
        the same "if history is unavailable ... start with an empty log" rule for a
        single entity rather than only for a total failure."""
        try:
            now = self._now_utc()
            window_start = now - timedelta(hours=self.retention_hours)

            events = []  # (ts, person_key, raw_state), merged across all persons
            current = {self._person_key(e): None for e in self.person_entities}
            for entity in self.person_entities:
                person = self._person_key(entity)
                try:
                    hist = self.get_history(entity_id=entity, start_time=window_start, end_time=now)
                except Exception as e:
                    self.log(
                        f"ActorAttribution backfill: get_history({entity}) failed: {e} - "
                        f"{person} stays unobservable until a live update arrives",
                        level="WARNING",
                    )
                    continue
                hist = self._flatten_history(hist, entity)
                if not hist:
                    self.log(
                        f"ActorAttribution backfill: no history returned for {entity} - "
                        f"{person} stays unobservable until a live update arrives",
                        level="WARNING",
                    )
                    continue
                first = True
                for entry in hist:
                    ts = _parse_utc(entry.get("last_changed") or entry.get("last_updated"))
                    if ts is None:
                        continue
                    state = entry.get("state")
                    if first:
                        # This row is whatever was already true when our window opens. If
                        # HA reports it from further back than window_start, that state
                        # holds continuously INTO window_start too (safe: last_changed
                        # already means nothing changed since). If this is the entity's
                        # actual first-ever change and it happens to land inside the
                        # window, do NOT pull it earlier than its real timestamp - we do
                        # not know what (if anything) came before it.
                        ts = window_start if ts <= window_start else ts
                        first = False
                    events.append((ts, person, state))

            door_open_events = []
            last_door_state = None
            door_hist = []
            try:
                door_hist = self.get_history(entity_id=self.door_entity, start_time=window_start, end_time=now)
                door_hist = self._flatten_history(door_hist, self.door_entity)
            except Exception as e:
                self.log(f"ActorAttribution backfill: get_history({self.door_entity}) failed: {e}", level="WARNING")
            first_door_row = True
            for entry in door_hist:
                ts = _parse_utc(entry.get("last_changed") or entry.get("last_updated"))
                state = entry.get("state")
                if ts is None:
                    continue
                # Skip edge detection on the very first row: we don't know what preceded
                # it, so it is a known STATE, not a provable fresh OPEN edge.
                if not first_door_row and state == "on" and last_door_state != "on":
                    door_open_events.append(ts)
                last_door_state = state
                first_door_row = False
            if door_hist:
                self._door_state = last_door_state

            events.sort(key=lambda e: e[0])
            log_entries = []
            last_pair = None
            i = 0
            while i < len(events):
                ts = events[i][0]
                while i < len(events) and events[i][0] == ts:
                    _, person, state = events[i]
                    current[person] = state
                    i += 1
                home = frozenset(p for p, s in current.items() if s == "home")
                unobservable = frozenset(p for p, s in current.items() if s in _UNOBSERVABLE_STATES)
                pair = (home, unobservable)
                if pair != last_pair:
                    log_entries.append(_Snapshot(ts, home, unobservable))
                    last_pair = pair

            self._person_state = current
            self._log = tuple(log_entries)
            self._door_events = tuple(sorted(door_open_events))
            self._prune_log(now)
            self._prune_door_events(now)

            self.log(
                f"ActorAttribution backfill complete: {len(self._log)} presence "
                f"transition(s), {len(self._door_events)} door-open event(s) over the "
                f"last {self.retention_hours:.0f}h",
                level="INFO",
            )
            self._publish()
        except Exception as e:
            self.log(f"ActorAttribution backfill failed, starting from an empty log: {e}", level="WARNING")

    def _flatten_history(self, hist, entity_id=None):
        """AppDaemon get_history returns list[list[dict]] (or occasionally dict).
        Normalize to list[dict]. Same idiom as washer_monitor.py/dryer_monitor.py's own
        copies - duplicated locally per this repo's convention rather than imported
        across app directories."""
        if isinstance(hist, dict):
            if entity_id is not None:
                hist = hist.get(entity_id, []) or hist.get("history", [])
            else:
                hist = next(iter(hist.values()), [])
        if isinstance(hist, list):
            if hist and isinstance(hist[0], list):
                return hist[0]
            return hist
        return []

    def _append_snapshot_if_changed(self, ts):
        """Recompute the (home, unobservable) pair from self._person_state and append a
        new log entry only if it actually differs from the last one - this is what keeps
        the log to "one entry per real transition" instead of growing on every tick
        regardless of content."""
        home = frozenset(p for p, s in self._person_state.items() if s == "home")
        unobservable = frozenset(p for p, s in self._person_state.items() if s in _UNOBSERVABLE_STATES)
        if self._log and self._log[-1].home == home and self._log[-1].unobservable == unobservable:
            return False
        self._log = self._log + (_Snapshot(ts, home, unobservable),)
        self._prune_log(ts)
        return True

    def _prune_log(self, now=None):
        """Keep everything needed to answer any anchor still valid under rule 1 (anchor
        >= now - retention_hours): that anchor's OWN stability window reaches back an
        extra stability_seconds beyond the plain retention boundary, so the prune
        cutoff must reach back that far too, or _snapshots_covering_window would
        wrongly report no_history right at the retention edge. Keeps one carry-in entry
        older than the cutoff (whatever state was already in effect going into the
        retained span) plus everything at/after it. Always replaces self._log wholesale
        with a new tuple - never mutates the old one in place, since AppDaemon calls
        listen_state callbacks from worker threads that might be iterating it."""
        now = now or self._now_utc()
        cutoff = now - timedelta(hours=self.retention_hours) - timedelta(seconds=self.stability_seconds)
        kept = tuple(e for e in self._log if e.ts >= cutoff)
        older = [e for e in self._log if e.ts < cutoff]
        if older:
            kept = (older[-1],) + kept
        self._log = kept

    def _prune_door_events(self, now=None):
        """Same reasoning as _prune_log but for door_settle_seconds instead of
        stability_seconds - no carry-in needed here since door events are point-in-time
        markers, not an ongoing state (rule 4 only asks "did an open edge fall in the
        window", never "what was the door's state at some earlier point")."""
        now = now or self._now_utc()
        cutoff = now - timedelta(hours=self.retention_hours) - timedelta(seconds=self.door_settle_seconds)
        self._door_events = tuple(t for t in self._door_events if t >= cutoff)

    def _publish(self):
        """Publish sensor.household_actor on every change and on every tick, following
        the middle-layer convention in presence_model.py/entry_truth.py. State is never
        empty (a dashboard/history-graph-hostile value) - always the attributed person's
        key or one of _REASON_TO_STATE's short words."""
        try:
            verdict = self.probe()
            if verdict["method"] == "sole_occupant":
                state = verdict["person"]
            else:
                state = _REASON_TO_STATE.get(verdict["reason"], "unobservable")
            stable_since = self._log[-1].ts if self._log else None
            last_door_open = self._door_events[-1] if self._door_events else None
            self.set_state(
                self.publish_sensor,
                state=state,
                replace=True,
                attributes={
                    "friendly_name": "Household actor",
                    "people_home": verdict["people_home"],
                    # "" sentinel, not None: AppDaemon 4.5.13's set_state silently drops
                    # any attribute equal to None/False/0 before the /api/states POST -
                    # see smart_cooling.py's _publish() for the mechanism - but "" survives.
                    "stable_since": self._iso(stable_since),
                    "last_door_open": self._iso(last_door_open),
                    "source_entities": list(self.person_entities) + [self.door_entity],
                    "computed_at": self._iso(self._now_utc()),
                },
            )
        except Exception as e:
            self.log(f"ActorAttribution publish failed: {e}", level="WARNING")

    def _now_utc(self):
        """Current time, timezone-aware UTC. Uses time.time() (not datetime.now()) to
        avoid any system-timezone mix-up - same reasoning/implementation as
        washer_monitor.py's _now_utc. Deliberately has zero AppDaemon dependency so
        tests can monkeypatch it directly."""
        return datetime.fromtimestamp(time.time(), timezone.utc)
