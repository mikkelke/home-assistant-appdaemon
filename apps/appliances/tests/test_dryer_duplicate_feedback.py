# tests/test_dryer_duplicate_feedback.py - Regression coverage for the 2026-08-12 finish_uld
# duplicate-feedback bug in dryer_monitor.py.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# Root cause: _transition_to_unemptied used to return None unconditionally and silently refuse
# (via _should_change_state) whenever the target state was already current OR still inside
# cooling_period. Every _save_cycle_feedback call site fired regardless of that refusal, so a
# finish_uld tail sitting in the low-power finish path (_confirm_finished) - re-polled roughly
# every 60s while watts stayed low - wrote a fresh junk feedback record on every refused
# re-entry: nine records in two bursts on the live box, one per poll, duration_min growing each
# time (5.1, 6.1, 7.1, 8.1, 9.1 minutes in the first burst - reproduced verbatim below).
#
# The fix has two layers, both exercised below:
#   1. _transition_to_unemptied (and, for consistency, its sibling _transition_to_* methods) now
#      returns True/False, and every _save_cycle_feedback call site gates on it.
#   2. _save_cycle_feedback itself refuses a second write for the same cycle_id as defence in
#      depth, in case some future call site forgets rule 1's gate.
#
# Following test_dryer_restart_survival.py's discipline: make_app runs the real initialize(),
# and neither _transition_to_unemptied nor _save_cycle_feedback is ever stubbed anywhere below -
# they are exactly the interaction under test.

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import timedelta
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

import dryer_monitor as dm  # noqa: E402

STATE_ENTITY = "sensor.dryer_state"
UI_SELECT = "input_select.dryer_state"
POWER_SENSOR = "sensor.dryer_plug_power"
ENERGY_SENSOR = "sensor.dryer_plug_energy"
DOOR_SENSOR = "binary_sensor.dryer_door_contact"


def _entity_get_state(entities):
    """get_state() double that distinguishes attribute="all" from a bare call - the same shape
    test_dryer_restart_survival.py uses, needed because initialize()'s real boot-resolution path
    reads the state entity both ways. See that file's own docstring for why the restore /
    transition path is never stubbed here either."""

    def get_state(entity, attribute=None, **kwargs):
        rec = entities.get(entity)
        if rec is None:
            return None
        if attribute == "all":
            return {
                "state": rec.get("state"),
                "attributes": dict(rec.get("attributes") or {}),
                "last_changed": rec.get("last_changed"),
                "last_updated": rec.get("last_changed"),
            }
        if attribute:
            return (rec.get("attributes") or {}).get(attribute)
        return rec.get("state")

    return get_state


def make_app(
    tmpdir,
    *,
    sensor_state=None,
    sensor_attrs=None,
    sensor_last_changed=None,
    helper_state=None,
    power_w="0",
    energy_kwh="10.0",
    door_state="off",
    extra_args=None,
):
    """Real DryerMonitor with initialize() run for real, and neither _transition_to_unemptied
    nor _save_cycle_feedback stubbed - see the module docstring. Copied from
    test_dryer_restart_survival.py's make_app (same tmpdir-scoping discipline: every test-only
    file - feedback, programmes, cycle-state store - lives under `tmpdir`, so nothing here ever
    touches, or races on, the real apps/appliances/dryer_*.json files)."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)

    entities = {
        STATE_ENTITY: {"state": sensor_state, "attributes": dict(sensor_attrs or {}), "last_changed": sensor_last_changed},
        UI_SELECT: {"state": helper_state, "attributes": {}, "last_changed": None},
        POWER_SENSOR: {"state": power_w, "attributes": {}, "last_changed": None},
        ENERGY_SENSOR: {"state": energy_kwh, "attributes": {}, "last_changed": None},
        DOOR_SENSOR: {"state": door_state, "attributes": {}, "last_changed": None},
    }

    app.get_state = _entity_get_state(entities)
    app.get_history = lambda **kwargs: []

    state_file = str(Path(tmpdir) / "dryer_cycle_state.json")
    args = {
        "power_sensor": POWER_SENSOR,
        "energy_sensor": ENERGY_SENSOR,
        "door_sensor": DOOR_SENSOR,
        "state_entity": STATE_ENTITY,
        "ui_state_entity": UI_SELECT,
        "start_w": 8,
        "stop_w": 5,
        "run_for": 60,
        "stop_for": 60,
        "feedback_file": str(Path(tmpdir) / "dryer_feedback_test.json"),
        "programmes_file": str(Path(tmpdir) / "dryer_programmes_test.yaml"),
        "state_file": state_file,
    }
    if extra_args:
        args.update(extra_args)
    app.args = args
    app.state_file = args["state_file"]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_app = lambda name: None
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None

    app.scheduled = []

    def run_in(cb, delay, **kw):
        handle = f"timer:{len(app.scheduled)}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in

    def run_every(cb, start, interval, **kw):
        handle = f"every:{len(app.scheduled)}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, interval, kw))
        return handle

    app.run_every = run_every
    app.datetime = lambda: app._now_utc()

    def set_state(entity_id, **kw):
        rec = entities.setdefault(entity_id, {"state": None, "attributes": {}, "last_changed": None})
        if "state" in kw and kw["state"] is not None:
            rec["state"] = kw["state"]
        if kw.get("attributes") is not None:
            if kw.get("replace"):
                rec["attributes"] = dict(kw["attributes"])
            else:
                rec["attributes"].update(kw["attributes"])
        rec["last_changed"] = app._now_utc()

    app.set_state = set_state

    return app, entities


def _freeze_now(app, when):
    """Monkeypatch app._now_utc to a fixed instant - every clock read in dryer_monitor.py goes
    through self._now_utc(), so this alone is enough to simulate poll-to-poll time passing
    without a real sleep. `when` must be tz-aware (the real _now_utc() always is)."""
    app._now_utc = lambda: when


def _read_feedback_cycles(app):
    """The on-disk feedback file's cycles list, or [] if nothing has been written yet."""
    path = Path(app.feedback_file)
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f).get("cycles", [])


class TheObservedFinishUldRepeatBug(unittest.TestCase):
    """Reproduces the 2026-08-12 live-box incident: a finish_uld-shaped tail (low energy, short
    run) sitting in the low-power finish path (_confirm_finished), with keep_fresh_detected
    already True from the main cycle's own anti-crease pattern - it is set once by
    _check_power_pattern and never cleared until the NEXT cycle starts (_confirm_running), so it
    is still True during the finish_uld tail. That is what lets _is_valid_completed_cycle bypass
    the 60min/0.05kWh floor for a cycle this short in the first place.

    Polled five times at the observed ~60s cadence - 5.1, 6.1, 7.1, 8.1, 9.1 minutes after cycle
    start, the exact durations logged on the box - every poll lands inside the 600s
    cooling_period from cycle start, so _should_change_state("Unemptied") is refused every time.
    Before the fix, _save_cycle_feedback fired regardless of that refusal."""

    def test_five_polls_inside_the_cooling_window_write_at_most_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0", energy_kwh="10.0")
            app.initialize()
            self.assertEqual(app.state, "Off")

            entities[POWER_SENSOR]["state"] = "900"  # >= start_w
            app._confirm_running({})
            self.assertEqual(app.state, "Running")
            self.assertIsNotNone(app.cycle_id)

            # See the class docstring: this is what actually happened in production - the main
            # cycle's own anti-crease pattern already set this flag, and it is never cleared
            # until a new cycle starts.
            app.keep_fresh_detected = True

            start_time = app.start_time
            base_now = app._now_utc()

            entities[POWER_SENSOR]["state"] = "0"       # <= stop_w
            entities[ENERGY_SENSOR]["state"] = "10.02"  # 0.02 kWh used - matches finish_uld

            for offset_min in (5.1, 6.1, 7.1, 8.1, 9.1):
                _freeze_now(app, base_now + timedelta(minutes=offset_min))
                app._confirm_finished({})

            self.assertEqual(app.state, "Running", "must not flip-flop out of Running mid-burst")
            self.assertEqual(entities[STATE_ENTITY]["state"], "Running")
            self.assertEqual(app.start_time, start_time, "cycle identity must not reset mid-burst")

            cycles = _read_feedback_cycles(app)
            self.assertLessEqual(len(cycles), 1, f"expected at most one feedback record, got {len(cycles)}: {cycles}")


class GenuineCompletedCycleStillWritesExactlyOneRecord(unittest.TestCase):
    """The fix must not touch the normal, successful path: a real (non finish_uld) cycle that
    clears both the 80% guard and cooling_period must still reach Unemptied and still save
    exactly one feedback record."""

    def test_genuine_cycle_reaches_unemptied_with_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0", energy_kwh="10.0")
            app.initialize()

            entities[POWER_SENSOR]["state"] = "900"
            app._confirm_running({})
            self.assertEqual(app.state, "Running")

            # 100 minutes in, 1.0 kWh used - past the 80% guard (96 min of the 120min "unknown"
            # fallback profile the test's minimal programme set classifies this as) and past
            # min_cycle_minutes/min_energy_kwh, without relying on keep_fresh_detected at all.
            app.start_time -= timedelta(minutes=100)
            # Clear the cooling window from cycle start too, so this is a genuinely SUCCEEDING
            # transition - the refused case is exercised separately below.
            app.last_state_change = app._now_utc() - timedelta(seconds=700)

            entities[POWER_SENSOR]["state"] = "0"
            entities[ENERGY_SENSOR]["state"] = "11.0"

            app._confirm_finished({})

            self.assertEqual(app.state, "Unemptied")
            cycles = _read_feedback_cycles(app)
            self.assertEqual(len(cycles), 1)
            self.assertEqual(cycles[0]["end_reason"], "low_power_detected")


class TransitionRefusedByCoolingPeriodWritesNoRecord(unittest.TestCase):
    """A generalisation of the observed bug beyond finish_uld specifically: ANY transition that
    _should_change_state refuses (here: still inside the 600s cooling window from cycle start -
    the same guard the observed bug hid behind) must save nothing at all - zero records, not
    "still only the one record from before this fix"."""

    def test_cooling_period_refusal_saves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0", energy_kwh="10.0")
            app.initialize()

            entities[POWER_SENSOR]["state"] = "900"
            app._confirm_running({})
            self.assertEqual(app.state, "Running")

            # Same shape as the genuine-cycle case above, but last_state_change is deliberately
            # left where _confirm_running stamped it (cycle start) - well inside the 600s
            # cooling_period.
            app.start_time -= timedelta(minutes=100)

            entities[POWER_SENSOR]["state"] = "0"
            entities[ENERGY_SENSOR]["state"] = "11.0"

            app._confirm_finished({})

            self.assertEqual(app.state, "Running", "a refused transition must leave state unchanged")
            self.assertEqual(_read_feedback_cycles(app), [])


class TwoDistinctCyclesEachWriteTheirOwnRecord(unittest.TestCase):
    """The per-cycle guard (rule 3) is keyed on cycle_id and must not leak across cycles: a
    second, later, genuinely different cycle must save its own record even though the FIRST
    cycle already has one on file, and must start with the guard cleared."""

    def test_second_cycle_is_not_suppressed_by_the_first_cycles_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0", energy_kwh="10.0")
            app.initialize()

            # --- Cycle 1: genuine completed cycle, reaches Unemptied, saves record 1. ---
            entities[POWER_SENSOR]["state"] = "900"
            app._confirm_running({})
            cycle_id_1 = app.cycle_id
            self.assertIsNotNone(cycle_id_1)

            app.start_time -= timedelta(minutes=100)
            app.last_state_change = app._now_utc() - timedelta(seconds=700)
            entities[POWER_SENSOR]["state"] = "0"
            entities[ENERGY_SENSOR]["state"] = "11.0"
            app._confirm_finished({})
            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(len(_read_feedback_cycles(app)), 1)

            # Wrap cycle 1 up for real (door open -> Emptied -> door close -> Off) - the only
            # way a NEW cycle is ever allowed to start (start detection requires state == "Off").
            app._transition_to_emptied("test: emptying cycle 1")
            app._transition_to_off("test: cycle 1 emptied", force=True)
            self.assertEqual(app.state, "Off")

            # --- Cycle 2: a second, distinct cycle. ---
            # Clear the cooling window before starting cycle 2: _confirm_running's own
            # _should_change_state("Running") call is unforced, so chaining a second cycle with
            # no real time passing would otherwise be refused by that SAME cooling_period -
            # unrelated to the per-cycle guard this test is actually about.
            app.last_state_change = app._now_utc() - timedelta(seconds=700)
            entities[POWER_SENSOR]["state"] = "900"
            entities[ENERGY_SENSOR]["state"] = "11.0"  # fresh energy_start baseline for cycle 2
            app._confirm_running({})
            cycle_id_2 = app.cycle_id
            self.assertIsNotNone(cycle_id_2)
            self.assertNotEqual(cycle_id_1, cycle_id_2, "a new cycle must mint a fresh cycle_id")
            self.assertIsNone(
                app._feedback_saved_cycle_id,
                "starting a new cycle must clear the duplicate-save guard",
            )

            app.start_time -= timedelta(minutes=100)
            app.last_state_change = app._now_utc() - timedelta(seconds=700)
            entities[POWER_SENSOR]["state"] = "0"
            entities[ENERGY_SENSOR]["state"] = "12.0"
            app._confirm_finished({})

            self.assertEqual(app.state, "Unemptied")
            cycles = _read_feedback_cycles(app)
            self.assertEqual(len(cycles), 2, f"each distinct cycle must write its own record: {cycles}")


class SaveCycleFeedbackRefusesADirectDuplicateForTheSameCycle(unittest.TestCase):
    """Defence in depth, tested in isolation from rule 1's gate: even if a caller saved feedback
    twice for the SAME cycle_id without ever checking _transition_to_unemptied's return value,
    _save_cycle_feedback's own guard must still refuse the second write outright, and log the
    refusal at DEBUG so the condition stays visible."""

    def test_direct_second_call_for_same_cycle_id_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0", energy_kwh="10.0")
            app.initialize()

            entities[POWER_SENSOR]["state"] = "900"
            app._confirm_running({})
            self.assertIsNotNone(app.cycle_id)

            kwargs = dict(
                predicted="bomuld__skabstoert",
                confirmed="bomuld__skabstoert",
                duration_min=140.0,
                energy_kwh=1.5,
                max_power_w=850.0,
                programme_confirmed_by_human=False,
                end_reason="low_power_detected",
            )
            app._save_cycle_feedback(**kwargs)
            app._save_cycle_feedback(**kwargs)  # same cycle_id, called directly - must refuse

            cycles = _read_feedback_cycles(app)
            self.assertEqual(len(cycles), 1)
            debug_logs = [a for a, kw in app.log_calls if kw.get("level") == "DEBUG"]
            self.assertTrue(
                any("duplicate" in str(a[0]).lower() for a in debug_logs),
                "the refused duplicate save must be logged at DEBUG",
            )


if __name__ == "__main__":
    unittest.main()
