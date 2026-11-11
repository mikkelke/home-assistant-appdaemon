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

A programme confirmation names the starter too (method programme_confirmed): whoever picks the
programme in the dashboard is taken to have loaded the machine. It only fills a starter nobody
named yet - the running cycle's, or after the finish the newest record's - and a later follow-up
never replaces a name already held. A monitor calls _listen_confirmer for its programme selector.

A host with ACTOR_PAIRED (washer, dryer) takes whoever loaded the machine to be the one who empties
it, and the other way round: a final verdict that names nobody gets the other side's name, method
same_as_starter or same_as_emptier. A pending emptying verdict waits for its follow-up first.
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


# Selector values that are not a human's pick: blanks and the "Auto (unconfirmed)" family.
_UNCONFIRMED_VALUES = ("", "—", "unknown", "unavailable", "none")


class ActorFollowupMixin:
    # Past pending_until, so the question is asked after the instant it names and not on it.
    ACTOR_FOLLOWUP_SLACK_S = 5
    ACTOR_PAIRED = False

    def _paired_actor(self, verdict, person, method):
        """`verdict` naming `person` by `method` when it names nobody, else `verdict`. The caller
        makes sure the verdict is final; a start verdict is, once the machine is emptied."""
        verdict = verdict or {}
        if not self.ACTOR_PAIRED or not person or verdict.get("person"):
            return verdict
        paired = dict(verdict, person=person, method=method)
        paired.pop("pending_until", None)
        return paired

    def _pair_emptier(self, actor_empty, record_ts=None, saved=True):
        """Pair the emptying verdict with the cycle's starter, before both are written.

        The starter is _cycle_actor, else (when the cycle is already saved) the feedback record
        this emptying will patch, record_ts or else the newest, while it is not yet emptied.
        Returns (actor_empty, actor_start): actor_start is the starter verdict newly named after
        the emptier, or None. That one is also set as _cycle_actor."""
        named = (self._cycle_actor or {}).get("person")
        record = self._unemptied_record(record_ts) if saved and not named else None
        starter = (self._cycle_actor or {}).get("person") or (record or {}).get("started_by")
        actor_empty = actor_empty or {}
        if actor_empty.get("pending_until"):
            return actor_empty, None
        actor_empty = self._paired_actor(actor_empty, starter, "same_as_starter")
        if starter or not actor_empty.get("person"):
            return actor_empty, None
        base = self._cycle_actor or ((record or {}).get("attribution") or {}).get("start")
        actor_start = self._paired_actor(base, actor_empty["person"], "same_as_emptier")
        if not actor_start.get("person"):
            return actor_empty, None
        self._cycle_actor = actor_start
        return actor_empty, actor_start

    def _unemptied_record(self, ts=None):
        """The feedback record saved at ts, else the newest, unless it already carries emptied_ts."""
        if not self.ACTOR_PAIRED:
            return None
        path = getattr(self, "feedback_file", None)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cycles = (json.load(f) or {}).get("cycles") or []
        except Exception:
            return None
        record = next((c for c in cycles if c.get("ts") == ts), None) if ts else (cycles[-1] if cycles else None)
        if not isinstance(record, dict) or record.get("emptied_ts"):
            return None
        return record

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
            if (self._cycle_actor or {}).get("person"):
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
            attrs = self._patch_empty_attribution(anchor, verdict)
            if attrs is None:
                return
            self.log(f"Emptied attribution settled: {attrs}", level="INFO")
            if attrs:
                self._publish_actor_attrs(attrs)
        except Exception as e:
            self.log(f"Emptied attribution follow-up failed: {e}", level="WARNING")

    def _listen_confirmer(self, entity, event):
        """Name the starter from whoever confirms the programme on `entity`. state_changed rather
        than listen_state, so context.user_id survives. `event` is the start event, e.g.
        washer_start."""
        if entity:
            self.listen_event(self._on_confirmer, "state_changed", entity_id=entity, actor_event=event)

    def _on_confirmer(self, event_name, data, kwargs):
        try:
            new = (data or {}).get("new_state") or {}
            old = (data or {}).get("old_state") or {}
            value = str(new.get("state") or "").strip()
            if value.lower() in _UNCONFIRMED_VALUES or value.lower().startswith("auto") or value == old.get("state"):
                return
            user_id = (new.get("context") or {}).get("user_id")
            if not user_id:
                return
            verdict = self._attribute(kwargs.get("actor_event"), user_id=user_id)
            if verdict.get("method") != "dashboard_action" or not verdict.get("person"):
                return
            if self.state in ("Running", "Paused"):
                if (self._cycle_actor or {}).get("person"):
                    return
                anchor = (self._cycle_actor or {}).get("anchor") or verdict.get("anchor")
                self._cycle_actor = dict(verdict, method="programme_confirmed", anchor=anchor)
            elif not self._patch_start_attribution(dict(verdict, method="programme_confirmed")):
                return
            self.log(f"Starter named by programme confirmation: {verdict['person']}", level="INFO")
            if self.state != "Off":
                self._publish_actor_attrs({"started_by": verdict["person"], "started_by_method": "programme_confirmed"})
        except Exception as e:
            self.log(f"Programme-confirmation starter failed: {e}", level="WARNING")

    def _patch_start_attribution(self, verdict):
        """Write `verdict` as started_by and attribution.start on the newest feedback record when
        it still names no starter, keeping that record's own anchor. True when it changed."""
        path = getattr(self, "feedback_file", None)
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Confirmation starter: could not read {path}: {e}", level="DEBUG")
            return False
        cycles = data.get("cycles") if isinstance(data, dict) else None
        record = cycles[-1] if isinstance(cycles, list) and cycles else None
        if not isinstance(record, dict) or record.get("started_by"):
            return False
        attribution = record.get("attribution") if isinstance(record.get("attribution"), dict) else {}
        anchor = (attribution.get("start") or {}).get("anchor") or verdict.get("anchor")
        attribution["start"] = dict(verdict, anchor=anchor)
        record["attribution"] = attribution
        record["started_by"] = verdict["person"]
        try:
            feedback_io.write_json(path, data)
        except Exception as e:
            self.log(f"Confirmation starter: could not write {path}: {e}", level="WARNING")
            return False
        return True

    def _publish_actor_attrs(self, attrs):
        """Merge `attrs` into the state entity without touching the state. Never raises."""
        try:
            self._set_state_entity(state=self.state, attributes=attrs)
        except Exception as e:
            self.log(f"Actor attributes {sorted(attrs)} could not be published: {e}", level="WARNING")

    def _patch_empty_attribution(self, anchor, verdict):
        """Write `verdict` as emptied_by and attribution.empty on the newest feedback record, but
        only when that record's attribution.empty has this anchor and its emptied_by is still
        null. emptied_ts is never written. With ACTOR_PAIRED the verdict is paired with the
        record's starter (see _paired_actor). Returns the name attributes to publish (empty when
        nobody is named), or None when the record was not changed."""
        path = getattr(self, "feedback_file", None)
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"Emptied follow-up: could not read {path}: {e}", level="DEBUG")
            return None
        cycles = data.get("cycles") if isinstance(data, dict) else None
        record = cycles[-1] if isinstance(cycles, list) and cycles else None
        attribution = record.get("attribution") if isinstance(record, dict) else None
        empty = attribution.get("empty") if isinstance(attribution, dict) else None
        if not isinstance(empty, dict) or empty.get("anchor") != anchor or record.get("emptied_by") is not None:
            self.log("Emptied follow-up: the newest record is not this emptying's - leaving it", level="DEBUG")
            return None
        verdict = self._paired_actor(verdict, record.get("started_by"), "same_as_starter")
        record["emptied_by"] = verdict.get("person")
        attribution["empty"] = verdict
        attrs = {}
        if verdict.get("person"):
            attrs = {"emptied_by": verdict["person"], "emptied_by_method": verdict.get("method") or "unknown"}
            start = self._paired_actor(attribution.get("start"), verdict["person"], "same_as_emptier")
            if start.get("method") == "same_as_emptier" and not record.get("started_by"):
                attribution["start"] = start
                record["started_by"] = start["person"]
                attrs.update(started_by=start["person"], started_by_method="same_as_emptier")
        try:
            feedback_io.write_json(path, data)
        except Exception as e:
            self.log(f"Emptied follow-up: could not write {path}: {e}", level="WARNING")
            return None
        return attrs
