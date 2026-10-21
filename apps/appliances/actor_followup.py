"""
actor_followup.py - names that settle after the event, for the washer, dryer and dishwasher
monitors.

ActorAttribution cannot always answer when the event happens: with two or more people home it
names someone by room elimination only once the others have stayed in their rooms for the hold
window, so a verdict asked for sooner is unknown and carries pending_until. A monitor that gets
one calls _follow_up_actor, which asks again at that instant with the same anchor, and a name
that settles then reaches every place the unknown was written: the cycle's start verdict
(_cycle_actor), the state entity, the cycle store, the feedback record.

The follow-up is a run_in callback of the monitor, so it runs on the monitor's own thread, between
its other callbacks, and it dies with the app on a reload. A restart during the wait just loses
the name: the record keeps its pending marker and nothing re-arms.

Only the attributes that carry the name are written, as a merge publish of the state the monitor
already holds. Never through a transition: that could rewrite the state or start a cooling clock.

A host class supplies: _attribute(event, at=, user_id=), _cycle_actor, state, feedback_file,
_cycle_store_cycle_id() and _set_state_entity() (CyclePersistenceMixin), _now_utc(), run_in, log.
Its store's trigger fields should include the starter, so a late name is written to the cycle
store at once instead of at the next heartbeat.
"""

import json
import math
import os

try:
    from cycle_store import parse_utc
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cycle_store import parse_utc

import feedback_io


class ActorFollowupMixin:
    # Past pending_until, so the question is asked after the instant it names and not on it.
    ACTOR_FOLLOWUP_SLACK_S = 5

    def _follow_up_actor(self, kind, verdict, event):
        """Ask ActorAttribution again once `verdict`, the answer for `event`, can be settled.

        kind is "start" (the cycle's starter) or "empty" (whoever emptied it). Does nothing
        unless the verdict carries pending_until. The callback gets the event, the verdict's
        anchor and the cycle id the verdict belongs to; it never re-arms itself. Never raises."""
        try:
            until = parse_utc((verdict or {}).get("pending_until"))
            anchor = (verdict or {}).get("anchor")
            if until is None or not anchor:
                return
            callback = {"start": self._actor_followup_start, "empty": self._actor_followup_empty}.get(kind)
            if callback is None:
                self.log(f"Actor follow-up: unknown kind {kind!r}", level="WARNING")
                return
            delay = max(1, math.ceil((until - self._now_utc()).total_seconds()) + self.ACTOR_FOLLOWUP_SLACK_S)
            self.run_in(
                callback,
                delay,
                actor_event=event,
                actor_anchor=anchor,
                actor_cycle_id=self._cycle_store_cycle_id(),
            )
            self.log(f"Actor follow-up ({kind}) for {event!r} in {delay}s", level="DEBUG")
        except Exception as e:
            self.log(f"Actor follow-up ({kind}) could not be scheduled: {e}", level="WARNING")

    def _actor_followup_start(self, kwargs):
        """The starter's second answer. Acts only while the cycle it was asked for is still
        running or paused: the verdict then replaces _cycle_actor, and a name is published. An
        unknown answer is kept as it is, so the record written at the finish carries its final
        reason instead of the pending one."""
        try:
            if kwargs.get("actor_cycle_id") != self._cycle_store_cycle_id():
                return
            if self.state not in ("Running", "Paused"):
                return
            at = parse_utc(kwargs.get("actor_anchor"))
            if at is None:
                return
            verdict = self._attribute(kwargs.get("actor_event"), at=at)
            self._cycle_actor = verdict
            self.log(f"Start attribution settled: {verdict}", level="INFO")
            if verdict.get("person"):
                self._publish_actor_attrs({
                    "started_by": verdict["person"],
                    "started_by_method": verdict.get("method") or "unknown",
                })
        except Exception as e:
            self.log(f"Start attribution follow-up failed: {e}", level="WARNING")

    def _actor_followup_empty(self, kwargs):
        """The emptier's second answer. The newest feedback record takes it only if it is the one
        written for this emptying and still names nobody; only then is a name published."""
        try:
            anchor = kwargs.get("actor_anchor")
            at = parse_utc(anchor)
            if at is None:
                return
            verdict = self._attribute(kwargs.get("actor_event"), at=at)
            if not self._patch_empty_attribution(anchor, verdict):
                return
            self.log(f"Emptied attribution settled: {verdict}", level="INFO")
            if verdict.get("person"):
                self._publish_actor_attrs({
                    "emptied_by": verdict["person"],
                    "emptied_by_method": verdict.get("method") or "unknown",
                })
        except Exception as e:
            self.log(f"Emptied attribution follow-up failed: {e}", level="WARNING")

    def _publish_actor_attrs(self, attrs):
        """Merge `attrs` into the state entity without touching the state. Never raises."""
        try:
            self._set_state_entity(state=self.state, attributes=attrs)
        except Exception as e:
            self.log(f"Actor attributes {sorted(attrs)} could not be published: {e}", level="WARNING")

    def _patch_empty_attribution(self, anchor, verdict):
        """Write `verdict` as emptied_by and attribution.empty on the newest feedback record, but
        only when that record's attribution.empty has this anchor and its emptied_by is still
        null. emptied_ts is never written. True when the record was changed."""
        path = getattr(self, "feedback_file", None)
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Emptied follow-up: could not read {path}: {e}", level="DEBUG")
            return False
        cycles = data.get("cycles") if isinstance(data, dict) else None
        record = cycles[-1] if isinstance(cycles, list) and cycles else None
        attribution = record.get("attribution") if isinstance(record, dict) else None
        empty = attribution.get("empty") if isinstance(attribution, dict) else None
        if not isinstance(empty, dict) or empty.get("anchor") != anchor or record.get("emptied_by") is not None:
            self.log("Emptied follow-up: the newest record is not this emptying's - leaving it", level="DEBUG")
            return False
        record["emptied_by"] = verdict.get("person")
        attribution["empty"] = verdict
        try:
            feedback_io.write_json(path, data)
        except Exception as e:
            self.log(f"Emptied follow-up: could not write {path}: {e}", level="WARNING")
            return False
        return True
