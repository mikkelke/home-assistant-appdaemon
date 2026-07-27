# tests/test_washer_attribution.py - "Who started this wash / who emptied it" unit tests for
# washer_monitor.py: the ActorAttribution resolver (_attribute), the restore-shape helper
# (_cycle_actor_from_state_attrs), the attribution anchor picked in _begin_running_cycle, the
# delayed-start slide NOT re-attributing, the actor_start/actor_empty fields written by
# _save_cycle_feedback, the emptied_by patch (_patch_cycle_record), and confirm-push targeting.
# Same __new__ + monkeypatched-callables harness as test_washer_vibration.py /
# test_washer_unavailable_grace.py in this directory (no running AppDaemon required).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import washer_monitor as wm  # noqa: E402

_RESULT_KEYS = {"person", "method", "reason", "people_home", "anchor", "evaluated_at", "version"}


def _base_app():
    """Minimal WasherMonitor without running AppDaemon's initialize() - just log/_now_utc,
    which every test below needs regardless of what else it stubs."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app._now_utc = lambda: datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
    return app


def _warning_logged(app):
    return any(kw.get("level") == "WARNING" for _a, kw in app.log_calls)


def _last_log_level(app):
    return app.log_calls[-1][1].get("level") if app.log_calls else None


class AttributeAppMissingOrFailing(unittest.TestCase):
    """_attribute() must never let an ActorAttribution problem break a cycle: get_app
    returning None, get_app raising, and the app's own attribute() raising all fall back to a
    dict in the exact shape a real result has (see ActorAttribution.attribute()'s documented
    contract) so callers can always do result.get("person") / result.get("method")."""

    def test_get_app_returns_none(self):
        app = _base_app()
        app.get_app = lambda name: None
        result = app._attribute("washer_start")
        self.assertEqual(set(result.keys()), _RESULT_KEYS)
        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")
        self.assertEqual(result["reason"], "attribution_app_missing")
        self.assertEqual(result["people_home"], [])
        self.assertEqual(result["version"], 1)
        self.assertTrue(_warning_logged(app))

    def test_get_app_raises(self):
        app = _base_app()

        def boom(name):
            raise RuntimeError("app registry unavailable")

        app.get_app = boom
        result = app._attribute("washer_start")
        self.assertEqual(set(result.keys()), _RESULT_KEYS)
        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "attribution_error")
        self.assertTrue(_warning_logged(app))

    def test_attribute_call_raises(self):
        app = _base_app()

        class BoomApp:
            def attribute(self, event, at=None):
                raise RuntimeError("boom")

        app.get_app = lambda name: BoomApp()
        result = app._attribute("washer_emptied")
        self.assertEqual(set(result.keys()), _RESULT_KEYS)
        self.assertIsNone(result["person"])
        self.assertEqual(result["reason"], "attribution_error")
        self.assertTrue(_warning_logged(app))

    def test_non_dict_result_is_treated_as_a_failure(self):
        app = _base_app()

        class WeirdApp:
            def attribute(self, event, at=None):
                return None  # frozen API says dict; guard against a misbehaving app anyway

        app.get_app = lambda name: WeirdApp()
        result = app._attribute("washer_start")
        self.assertEqual(set(result.keys()), _RESULT_KEYS)
        self.assertEqual(result["reason"], "attribution_error")

    def test_successful_call_is_passed_through_unchanged(self):
        app = _base_app()
        real_result = {
            "person": "mikkel", "method": "sole_occupant", "reason": None,
            "people_home": ["mikkel"], "anchor": "2026-07-27T10:00:00Z",
            "evaluated_at": "2026-07-27T12:00:00Z", "version": 1,
        }
        calls = []

        class RealApp:
            def attribute(self, event, at=None):
                calls.append((event, at))
                return real_result

        app.get_app = lambda name: RealApp()
        at = datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc)
        result = app._attribute("washer_start", at=at)
        self.assertIs(result, real_result)
        self.assertEqual(calls, [("washer_start", at)])


class CycleActorFromStateAttrs(unittest.TestCase):
    """_cycle_actor_from_state_attrs rebuilds a _cycle_actor-shaped dict from the started_by /
    started_by_method persisted on the state entity - used by _restore_running_state and
    _recover_from_false_unemptied, neither of which goes through _begin_running_cycle."""

    def test_person_and_method_carried_over(self):
        app = _base_app()
        result = app._cycle_actor_from_state_attrs({"started_by": "mikkel", "started_by_method": "sole_occupant"})
        self.assertEqual(set(result.keys()), _RESULT_KEYS)
        self.assertEqual(result["person"], "mikkel")
        self.assertEqual(result["method"], "sole_occupant")
        self.assertEqual(result["reason"], "restored_from_entity")

    def test_empty_string_treated_as_missing_not_a_real_value(self):
        """AppDaemon 4.5.13 drops attributes equal to None/False/0 from set_state - we publish ""
        instead - so "" read back must mean "nothing was there", not a literal empty person."""
        app = _base_app()
        result = app._cycle_actor_from_state_attrs({"started_by": "", "started_by_method": ""})
        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")

    def test_missing_keys_treated_as_missing(self):
        app = _base_app()
        result = app._cycle_actor_from_state_attrs({})
        self.assertIsNone(result["person"])
        self.assertEqual(result["method"], "unknown")
        self.assertEqual(result["reason"], "restored_from_entity")


def _make_begin_cycle_app(now, last_door_closed_at=None, last_door_closed_trusted=False, last_off_at_str=None):
    """WasherMonitor stubbed just enough to run the real _begin_running_cycle end to end -
    everything it touches besides the attribution anchor (poll timers, energy sensor, HA
    entity plumbing) is faked out; use_energy_detection=False so _start_energy_detection (a
    separate concern) is never reached."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app._now_utc = lambda: now
    app.state_entity = "sensor.washer_state"
    app.energy_sensor = "sensor.washer_plug_energy"
    app.confirm_entity = None
    app.pause_window_minutes = 3
    app.max_running_hours = 5
    app.use_energy_detection = False
    app.args = {}
    app.last_door_closed_trusted = last_door_closed_trusted
    app.last_door_closed_at = last_door_closed_at
    app.energy_start = None  # no numeric energy_sensor reading configured in this fixture

    states = {}
    if last_off_at_str is not None:
        states[("sensor.washer_state", "last_off_at")] = last_off_at_str

    def get_state(entity, attribute=None, **kw):
        if attribute is None:
            return states.get(entity)
        return states.get((entity, attribute))

    app.get_state = get_state

    app.attribute_calls = []

    def fake_attribute(event, at=None):
        app.attribute_calls.append((event, at))
        return {
            "person": "mikkel", "method": "sole_occupant", "reason": None,
            "people_home": ["mikkel"], "anchor": "x", "evaluated_at": "y", "version": 1,
        }

    app._attribute = fake_attribute

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app.set_state_calls = []
    app._set_state_entity = lambda **kw: app.set_state_calls.append(kw)

    app.scheduled = []

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    app.run_in = run_in
    app.poll_timer = None
    app.history_poll_timer = None
    app.running_watchdog_timer = None
    return app


class BeginRunningCycleAttributionAnchor(unittest.TestCase):
    """_begin_running_cycle anchors attribution to when the machine was LOADED (a trusted,
    fresh door close), not to now (when the motor happens to start) - see the docstring at the
    call site for the delayed-start rationale."""

    def test_fresh_trusted_door_close_is_used_as_anchor(self):
        now = datetime(2026, 7, 27, 20, 0, 0, tzinfo=timezone.utc)
        door_close = now - timedelta(hours=2)
        app = _make_begin_cycle_app(
            now, last_door_closed_at=door_close, last_door_closed_trusted=True,
            last_off_at_str=(now - timedelta(hours=3)).isoformat(),
        )
        app._begin_running_cycle()
        self.assertEqual(app.attribute_calls, [("washer_start", door_close)])
        self.assertEqual(app.set_state_calls[-1]["attributes"]["started_by"], "mikkel")
        self.assertEqual(app.set_state_calls[-1]["attributes"]["started_by_method"], "sole_occupant")

    def test_stale_door_close_over_12h_falls_back_to_now(self):
        now = datetime(2026, 7, 27, 20, 0, 0, tzinfo=timezone.utc)
        door_close = now - timedelta(hours=13)
        app = _make_begin_cycle_app(
            now, last_door_closed_at=door_close, last_door_closed_trusted=True,
            last_off_at_str=(now - timedelta(hours=14)).isoformat(),
        )
        app._begin_running_cycle()
        self.assertEqual(app.attribute_calls, [("washer_start", now)])

    def test_untrusted_door_close_falls_back_to_now(self):
        now = datetime(2026, 7, 27, 20, 0, 0, tzinfo=timezone.utc)
        door_close = now - timedelta(hours=1)  # fresh, but not trusted (e.g. restart artefact)
        app = _make_begin_cycle_app(now, last_door_closed_at=door_close, last_door_closed_trusted=False)
        app._begin_running_cycle()
        self.assertEqual(app.attribute_calls, [("washer_start", now)])

    def test_door_close_before_last_off_falls_back_to_now(self):
        """A trusted, fresh door close that is nonetheless stale from a PREVIOUS cycle (it
        happened before we last went Off) must not anchor the new cycle's attribution."""
        now = datetime(2026, 7, 27, 20, 0, 0, tzinfo=timezone.utc)
        door_close = now - timedelta(hours=1)
        last_off = now - timedelta(minutes=30)  # after the door close -> door close predates it
        app = _make_begin_cycle_app(
            now, last_door_closed_at=door_close, last_door_closed_trusted=True,
            last_off_at_str=last_off.isoformat(),
        )
        app._begin_running_cycle()
        self.assertEqual(app.attribute_calls, [("washer_start", now)])


class DelayedStartSlideDoesNotReattribute(unittest.TestCase):
    """_slide_start_for_delayed_start moves start_time forward to when the motor actually
    starts, but must NOT re-run attribution - the anchor is when the machine was loaded, not
    when the (possibly hours-later) wash begins."""

    def test_cycle_actor_unchanged_across_the_slide(self):
        app = wm.WasherMonitor.__new__(wm.WasherMonitor)
        sentinel_actor = {
            "person": "mikkel", "method": "sole_occupant", "reason": None,
            "people_home": ["mikkel"], "anchor": "x", "evaluated_at": "y", "version": 1,
        }
        app._cycle_actor = sentinel_actor
        old_start = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)
        app.start_time = old_start
        app.energy_start = None
        resume_at = old_start + timedelta(minutes=365)

        app.energy_sensor = "sensor.washer_plug_energy"
        app.get_state = lambda entity, **kw: None
        app.log_calls = []
        app.log = lambda *a, **kw: app.log_calls.append((a, kw))
        app._push_corrected_start_time_to_entity = lambda: None
        app.running_watchdog_timer = None
        app.max_running_hours = 5
        app.scheduled = []

        def run_in(cb, delay, **kw):
            app.scheduled.append((cb, delay, kw))
            return object()

        app.run_in = run_in

        app._slide_start_for_delayed_start(resume_at)

        self.assertIs(app._cycle_actor, sentinel_actor)
        self.assertEqual(app.start_time, resume_at)
        self.assertTrue(app._delayed_start_trimmed)


class SaveCycleFeedbackActorFields(unittest.TestCase):
    """_save_cycle_feedback writes started_by/attribution.start on every save (plain JSON null
    when unknown - the AppDaemon None-dropping bug only applies to HA entity attributes, not
    this file) and emptied_by/emptied_ts/attribution.empty only when actor_empty is given."""

    def _make_app(self):
        app = _base_app()
        app.feedback_file = os.path.join(tempfile.mkdtemp(), "feedback.json")
        self.addCleanup(lambda: os.path.exists(app.feedback_file) and os.remove(app.feedback_file))
        app._learned_durations = {}
        app._history_centroids = {}
        return app

    def _save(self, app, **overrides):
        kwargs = dict(
            predicted="bomuld", predicted_temperature="40°C",
            confirmed="bomuld", confirmed_temperature="40°C",
            duration_min=159.0, energy_kwh=0.75, heating_bursts=1, max_power_w=2000.0,
        )
        kwargs.update(overrides)
        return app._save_cycle_feedback(**kwargs)

    def test_started_by_and_attribution_start_written_when_known(self):
        app = self._make_app()
        actor = {
            "person": "mikkel", "method": "sole_occupant", "reason": None,
            "people_home": ["mikkel"], "anchor": "x", "evaluated_at": "y", "version": 1,
        }
        record = self._save(app, actor_start=actor)
        self.assertEqual(record["started_by"], "mikkel")
        self.assertEqual(record["attribution"], {"start": actor})
        with open(app.feedback_file) as f:
            saved = json.load(f)
        self.assertEqual(saved["cycles"][0]["started_by"], "mikkel")

    def test_started_by_is_json_null_not_absent_or_empty_string_when_unknown(self):
        app = self._make_app()
        self._save(app, actor_start=None)
        with open(app.feedback_file) as f:
            raw = f.read()
        saved = json.loads(raw)
        rec = saved["cycles"][0]
        self.assertIn("started_by", rec)
        self.assertIsNone(rec["started_by"])
        self.assertNotEqual(rec.get("started_by"), "")
        self.assertIn('"started_by": null', raw)

    def test_actor_empty_writes_emptied_fields_when_given(self):
        app = self._make_app()
        actor_start = {"person": "mikkel", "method": "sole_occupant"}
        actor_empty = {"person": "kristine", "method": "sole_occupant"}
        record = self._save(app, actor_start=actor_start, actor_empty=actor_empty)
        self.assertEqual(record["emptied_by"], "kristine")
        self.assertIn("emptied_ts", record)
        self.assertEqual(record["attribution"], {"start": actor_start, "empty": actor_empty})

    def test_emptied_fields_absent_when_actor_empty_not_given(self):
        """The far more common Unemptied-first save: emptying hasn't been observed yet, so
        these keys must not appear at all (not even as null) until _patch_cycle_record adds
        them later."""
        app = self._make_app()
        record = self._save(app, actor_start={"person": "mikkel"})
        self.assertNotIn("emptied_by", record)
        self.assertNotIn("emptied_ts", record)
        self.assertNotIn("empty", record["attribution"])


class PatchCycleRecordTests(unittest.TestCase):
    """_patch_cycle_record (Path B: the record already exists from _transition_to_unemptied,
    someone opened the door later) writes emptied_by/emptied_ts onto the matching record and
    merges into "attribution" rather than clobbering the "start" key already there."""

    def _write_feedback(self, cycles):
        path = os.path.join(tempfile.mkdtemp(), "feedback.json")
        with open(path, "w") as f:
            json.dump({"version": 2, "cycles": cycles}, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_patches_matching_record_and_merges_attribution(self):
        app = _base_app()
        app.feedback_file = self._write_feedback([
            {"ts": "a", "confirmed": "eco", "started_by": "mikkel", "attribution": {"start": {"person": "mikkel"}}},
            {"ts": "b", "confirmed": "bomuld"},
        ])
        app._patch_cycle_record("a", {
            "emptied_by": "kristine",
            "emptied_ts": "2026-07-27T14:00:00+02:00",
            "attribution": {"empty": {"person": "kristine"}},
        })
        with open(app.feedback_file) as f:
            data = json.load(f)
        target = next(c for c in data["cycles"] if c["ts"] == "a")
        other = next(c for c in data["cycles"] if c["ts"] == "b")
        self.assertEqual(target["emptied_by"], "kristine")
        self.assertEqual(target["emptied_ts"], "2026-07-27T14:00:00+02:00")
        # "start" survives the patch - only "empty" is added alongside it.
        self.assertEqual(target["attribution"], {"start": {"person": "mikkel"}, "empty": {"person": "kristine"}})
        self.assertNotIn("emptied_by", other)

    def test_no_ts_falls_back_to_newest_record(self):
        # The common real case: Unemptied outlives the process (it lasts until a human walks
        # over, and this box gets redeployed constantly), so _last_saved_record_ts is None by
        # the time the door finally opens. The newest record is the right target - a later
        # cycle cannot have completed while the machine sat Unemptied.
        app = _base_app()
        app.feedback_file = self._write_feedback([
            {"ts": "old", "confirmed": "eco", "emptied_ts": "2026-07-26T09:00:00+02:00"},
            {"ts": "newest", "confirmed": "bomuld", "attribution": {"start": {"person": "mikkel"}}},
        ])
        app._patch_cycle_record(None, {
            "emptied_by": "kristine",
            "emptied_ts": "2026-07-27T14:00:00+02:00",
            "attribution": {"empty": {"person": "kristine"}},
        })
        with open(app.feedback_file) as f:
            data = json.load(f)
        newest = next(c for c in data["cycles"] if c["ts"] == "newest")
        old = next(c for c in data["cycles"] if c["ts"] == "old")
        self.assertEqual(newest["emptied_by"], "kristine")
        self.assertEqual(newest["attribution"], {"start": {"person": "mikkel"}, "empty": {"person": "kristine"}})
        self.assertNotIn("emptied_by", old)

    def test_no_ts_does_not_restamp_an_already_emptied_record(self):
        # Idempotence: a second door-open must not overwrite who emptied it.
        app = _base_app()
        app.feedback_file = self._write_feedback([
            {"ts": "newest", "emptied_by": "mikkel", "emptied_ts": "2026-07-27T10:00:00+02:00"},
        ])
        app._patch_cycle_record(None, {
            "emptied_by": "kristine",
            "emptied_ts": "2026-07-27T14:00:00+02:00",
        })
        with open(app.feedback_file) as f:
            data = json.load(f)
        self.assertEqual(data["cycles"][0]["emptied_by"], "mikkel")
        self.assertEqual(data["cycles"][0]["emptied_ts"], "2026-07-27T10:00:00+02:00")

    def test_stale_ts_pointing_at_an_emptied_cycle_is_not_restamped(self):
        # _last_saved_record_ts is only assigned when a save returns a record, so a failed save
        # leaves the PREVIOUS cycle's ts in place. Crediting this emptying to that older cycle
        # would be a silent mis-attribution; an existing emptied_ts is the tell.
        app = _base_app()
        app.feedback_file = self._write_feedback([
            {"ts": "previous", "emptied_by": "mikkel", "emptied_ts": "2026-07-26T09:00:00+02:00"},
        ])
        app._patch_cycle_record("previous", {
            "emptied_by": "claudia",
            "emptied_ts": "2026-07-27T14:00:00+02:00",
        })
        with open(app.feedback_file) as f:
            data = json.load(f)
        self.assertEqual(data["cycles"][0]["emptied_by"], "mikkel")

    def test_no_ts_and_no_records_is_a_clean_noop(self):
        app = _base_app()
        app.feedback_file = self._write_feedback([])
        app._patch_cycle_record(None, {"emptied_by": "mikkel"})  # must not raise
        self.assertEqual(_last_log_level(app), "DEBUG")

    def test_noop_when_ts_not_found(self):
        # A ts we were handed but cannot find came from a save that reported success, so it is
        # genuinely odd and logs louder than the benign skips.
        app = _base_app()
        app.feedback_file = self._write_feedback([{"ts": "z", "confirmed": "eco"}])
        app._patch_cycle_record("no-such-ts", {"emptied_by": "mikkel"})
        with open(app.feedback_file) as f:
            data = json.load(f)
        self.assertNotIn("emptied_by", data["cycles"][0])
        self.assertEqual(_last_log_level(app), "WARNING")

    def test_noop_when_feedback_file_missing(self):
        app = _base_app()
        app.feedback_file = "/nonexistent/path/should/not/exist.json"
        app._patch_cycle_record("a", {"emptied_by": "mikkel"})  # must not raise
        self.assertEqual(_last_log_level(app), "DEBUG")


def _make_confirm_push_app(confirm_push_target_actor=True, confirm_push_target="home"):
    app = _base_app()
    app.confirm_push_target_actor = confirm_push_target_actor
    app.confirm_push_target = confirm_push_target
    app.confirm_push_dashboard_uri = "/local/ha-dashboard/index.html"
    app.notify_calls = []

    class FakeNotifier:
        def notify(self, **kwargs):
            app.notify_calls.append(kwargs)
            return "notify-result-sentinel"

    app.get_app = lambda name: FakeNotifier() if name == "MobileNotifier" else None
    app.create_task = lambda x: x
    return app


class ConfirmPushTargeting(unittest.TestCase):
    """_send_confirm_push targets [started_by] directly (bypassing category_audience via
    MobileNotifier's list-target semantics) when confirm_push_target_actor is on and the
    record names who started the wash; otherwise it falls back to the usual broadcast."""

    _RECORD = {"ts": "2026-07-27T18:00:00+02:00", "predicted": "bomuld", "predicted_temperature": "40"}

    def test_targets_started_by_when_known_and_flag_enabled(self):
        app = _make_confirm_push_app(confirm_push_target_actor=True)
        record = dict(self._RECORD, started_by="mikkel")
        app._send_confirm_push(record)
        self.assertEqual(len(app.notify_calls), 1)
        self.assertEqual(app.notify_calls[0]["target"], ["mikkel"])

    def test_falls_back_to_broadcast_when_started_by_unknown(self):
        app = _make_confirm_push_app(confirm_push_target_actor=True)
        record = dict(self._RECORD, started_by=None)
        app._send_confirm_push(record)
        self.assertEqual(app.notify_calls[0]["target"], "home")

    def test_falls_back_to_broadcast_when_flag_disabled(self):
        """Flag-gated so this is revertible without a code change - even a known started_by
        must not be used when confirm_push_target_actor is false."""
        app = _make_confirm_push_app(confirm_push_target_actor=False)
        record = dict(self._RECORD, started_by="mikkel")
        app._send_confirm_push(record)
        self.assertEqual(app.notify_calls[0]["target"], "home")


if __name__ == "__main__":
    unittest.main()
