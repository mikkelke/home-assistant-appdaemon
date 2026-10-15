# tests/test_dishwasher_pick_after_finish.py - a programme or Short pick on the dishwasher card once the run
# has finished corrects that run's feedback record, as the washer's picker does for its last one: the
# hygiene run (Intensive) is logged that way, and HouseUpcoming's row counts from it. Only the run that has
# just finished can be corrected, by the monitor's own state and the time its record was saved.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_dishwasher_audit_2026_09 as audit  # noqa: E402  (installs the appdaemon stub)

dm = audit.dm

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
FINISHED = NOW - timedelta(hours=3)
SELECTOR = "input_select.dishwasher_confirmed_programme"
SHORT = "input_select.dishwasher_short"
STATE_ENTITY = "sensor.dishwasher_state"


def record(ts, confirmed="eco", human=False, **extra):
    rec = {"ts": ts.isoformat(), "duration_min": 229.2, "energy_kwh": 0.71, "predicted": "eco", "confirmed": confirmed,
           "programme_confirmed_by_human": human, "max_power_w": 1946.0}
    if confirmed in ("eco", "quick"):
        rec["short"] = False
    rec.update(extra)
    return rec


def make_app(cycles, state="Unemptied", state_since=FINISHED, programme="—", short="No"):
    """A monitor that has finished a run at `state_since` and saved the last record in `cycles` for it."""
    tmp = tempfile.mkdtemp(prefix="dishwasher_pick_")
    path = os.path.join(tmp, "dishwasher_feedback.json")
    with open(path, "w") as f:
        json.dump({"version": 2, "cycles": cycles}, f)
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
    app.feedback_file = path
    app.state_entity = STATE_ENTITY
    app.confirmed_programme_entity = SELECTOR
    app.short_entity = SHORT
    app._learned_durations = {}
    app.detected_short = False
    app.detected_quick_short = False
    app.state = state
    app.state_since = state_since
    app.logs = []
    app.log = lambda msg, level="INFO", **kw: app.logs.append((level, msg))
    app.states = {STATE_ENTITY: state, SELECTOR: programme, SHORT: short}
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app._now_utc = lambda: NOW
    return app, path


def saved(path):
    with open(path) as f:
        return json.load(f)["cycles"]


def pick(app, label, old="—"):
    """The programme selector changes to `label`; the callback gets the new value, as AppDaemon hands it."""
    app.states[SELECTOR] = label
    app._on_selector_changed(SELECTOR, "state", old, label, {})


def pick_short(app, label, old="No"):
    app.states[SHORT] = label
    app._on_selector_changed(SHORT, "state", old, label, {})


def ignored(app):
    return [msg for level, msg in app.logs if level == "INFO" and "ignored" in msg]


class PickAfterFinishTests(unittest.TestCase):
    def test_intensive_picked_while_unemptied_confirms_the_run_that_just_finished_as_the_hygiene_run(self):
        app, path = make_app([record(NOW - timedelta(days=5)), record(FINISHED)])
        pick(app, "Intensive")
        first, last = saved(path)
        self.assertEqual((first["confirmed"], first["programme_confirmed_by_human"]), ("eco", False))
        self.assertEqual((last["confirmed"], last["programme_confirmed_by_human"]), ("intensive", True))
        self.assertEqual(last["programme_confirmed_via"], "picked_after_finish")
        self.assertNotIn("short", last)
        self.assertEqual((last["ts"], last["duration_min"], last["energy_kwh"]), (FINISHED.isoformat(), 229.2, 0.71))
        self.assertEqual(ignored(app), [])

    def test_the_learned_durations_follow_the_file(self):
        app, path = make_app([record(FINISHED)])
        pick(app, "Intensive")
        self.assertEqual(app._learned_durations, {"intensive": {"n": 1, "avg": 229.2}})
        fresh, _ = make_app([])
        fresh.feedback_file = path
        fresh._load_and_apply_feedback()
        self.assertEqual(fresh._learned_durations, app._learned_durations)

    def test_a_record_saved_seconds_after_the_state_change_is_the_same_finish(self):
        for seconds in (0, 2, 90, -90, 299):
            app, path = make_app([record(FINISHED + timedelta(seconds=seconds))])
            pick(app, "Intensive")
            self.assertEqual(saved(path)[-1]["confirmed"], "intensive", seconds)

    def test_a_pick_while_the_run_is_going_is_left_to_the_finish_and_says_nothing(self):
        for state in ("Running", "Paused"):
            app, path = make_app([record(FINISHED)], state=state)
            pick(app, "Intensive")
            self.assertEqual(saved(path)[-1]["confirmed"], "eco", state)
            self.assertEqual(app.logs, [], state)

    def test_nothing_to_confirm_with_no_programme_picked(self):
        for label in ("—", "Not set", "unknown", "unavailable", None):
            app, path = make_app([record(FINISHED)])
            pick(app, label)
            self.assertEqual(saved(path)[-1]["confirmed"], "eco", label)
            self.assertEqual(app.logs, [], label)

    def test_an_unreadable_file_is_logged_and_left_alone(self):
        app, path = make_app([record(FINISHED)])
        Path(path).write_text("{not json")
        pick(app, "Intensive")
        self.assertEqual(Path(path).read_text(), "{not json")
        self.assertTrue(any(level == "WARNING" for level, _ in app.logs))
        os.remove(path)
        pick(app, "Intensive")

    def test_the_same_pick_twice_writes_nothing_more(self):
        app, path = make_app([record(FINISHED)])
        pick(app, "Intensive")
        before = Path(path).read_text()
        pick(app, "Intensive")
        self.assertEqual(Path(path).read_text(), before)


class OnlyTheRunThatJustFinishedTests(unittest.TestCase):
    def test_a_forced_unemptied_has_no_record_so_the_run_before_it_is_not_relabelled(self):
        app, path = make_app([record(NOW - timedelta(days=3))], state_since=NOW - timedelta(minutes=20))
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        self.assertEqual(len(ignored(app)), 1)
        self.assertIn("not of the run that just finished", ignored(app)[0])
        self.assertIn("'Intensive'", ignored(app)[0])

    def test_the_run_before_a_forced_one_within_the_hours_is_not_relabelled_either(self):
        app, path = make_app([record(NOW - timedelta(hours=9))], state_since=NOW - timedelta(hours=1))
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        self.assertEqual(len(ignored(app)), 1)

    def test_a_restart_between_the_state_change_and_the_save_leaves_the_run_before_alone(self):
        app, path = make_app([record(FINISHED - timedelta(days=1))])
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        self.assertEqual(len(ignored(app)), 1)

    def test_the_edge_of_the_match(self):
        app, path = make_app([record(FINISHED + timedelta(minutes=5, seconds=1))])
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        app, path = make_app([record(FINISHED - timedelta(minutes=5, seconds=1))])
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")

    def test_a_state_without_a_clock_matches_nothing(self):
        app, path = make_app([record(FINISHED)], state_since=None)
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        self.assertEqual(len(ignored(app)), 1)

    def test_a_record_without_a_usable_time_matches_nothing(self):
        for ts in (None, "", "yesterday", 5):
            rec = record(FINISHED)
            rec["ts"] = ts
            app, path = make_app([rec])
            pick(app, "Intensive")
            self.assertEqual(saved(path)[-1]["confirmed"], "eco", ts)
            self.assertEqual(len(ignored(app)), 1, ts)

    def test_an_empty_history_has_no_run_to_correct(self):
        app, path = make_app([])
        pick(app, "Intensive")
        self.assertEqual(saved(path), [])
        self.assertIn("no run on record", ignored(app)[0])

    def test_twelve_hours_after_the_run_it_is_too_late(self):
        app, path = make_app([record(NOW - timedelta(hours=11, minutes=59))], state_since=NOW - timedelta(hours=11, minutes=59))
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "intensive")
        app, path = make_app([record(NOW - timedelta(hours=12, minutes=1))], state_since=NOW - timedelta(hours=12, minutes=1))
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        self.assertIn("more than 12 h ago", ignored(app)[0])

    def test_once_it_has_been_emptied_the_run_is_closed_and_the_pick_says_so(self):
        for state in ("Emptied", "Off", "Error"):
            app, path = make_app([record(FINISHED)], state=state)
            pick(app, "Intensive")
            self.assertEqual(saved(path)[-1]["confirmed"], "eco", state)
            self.assertEqual(len(ignored(app)), 1, state)
            self.assertIn(f"the dishwasher is {state}", ignored(app)[0])

    def test_the_monitors_own_state_decides_not_the_entity_the_store_may_serve_stale(self):
        app, path = make_app([record(FINISHED)], state="Running")
        app.states[STATE_ENTITY] = "Unemptied"
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        self.assertEqual(app.logs, [])
        app, path = make_app([record(FINISHED)], state="Unemptied")
        app.states[STATE_ENTITY] = "Running"
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "intensive")

    def test_a_pick_after_the_next_run_has_started_never_reaches_the_run_before(self):
        app, path = make_app([record(FINISHED)], state="Running")
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")


class ProgrammeAndShortTests(unittest.TestCase):
    def test_a_correction_replaces_the_pick_and_unmakes_the_hygiene_run(self):
        app, path = make_app([record(FINISHED)])
        pick(app, "Intensive")
        pick(app, "ECO", old="Intensive")
        last = saved(path)[-1]
        self.assertEqual((last["confirmed"], last["programme_confirmed_by_human"], last["short"]), ("eco", True, False))
        self.assertEqual(app._learned_durations, {"eco": {"n": 1, "avg": 229.2}})

    def test_eco_picked_then_short_picked_records_a_short_eco(self):
        app, path = make_app([record(FINISHED, duration_min=74.0)], short="No")
        pick(app, "ECO")
        self.assertEqual((saved(path)[-1]["confirmed"], saved(path)[-1]["short"]), ("eco", False))
        pick_short(app, "Yes")
        last = saved(path)[-1]
        self.assertEqual((last["confirmed"], last["programme_confirmed_by_human"], last["short"]), ("eco", True, True))
        self.assertEqual(app._learned_durations, {"eco_short": {"n": 1, "avg": 74.0}})

    def test_short_picked_first_then_eco_records_a_short_eco(self):
        app, path = make_app([record(FINISHED, duration_min=74.0)], programme="—", short="No")
        pick_short(app, "Yes")
        self.assertEqual(saved(path)[-1]["confirmed"], "eco")
        self.assertFalse(saved(path)[-1]["programme_confirmed_by_human"])
        self.assertEqual(app.logs, [])
        pick(app, "ECO")
        last = saved(path)[-1]
        self.assertEqual((last["confirmed"], last["programme_confirmed_by_human"], last["short"]), ("eco", True, True))

    def test_the_same_programme_with_another_short_is_a_correction_not_a_repeat(self):
        app, path = make_app([record(FINISHED, human=True, short=False)], programme="ECO", short="No")
        pick_short(app, "Yes")
        self.assertEqual(saved(path)[-1]["short"], True)
        pick_short(app, "No", old="Yes")
        self.assertEqual(saved(path)[-1]["short"], False)

    def test_the_picks_own_value_is_used_when_the_stored_state_lags(self):
        app, path = make_app([record(FINISHED)], programme="—", short="No")
        app._on_selector_changed(SELECTOR, "state", "—", "Intensive", {})
        self.assertEqual(saved(path)[-1]["confirmed"], "intensive")
        app, path = make_app([record(FINISHED, human=True)], programme="ECO", short="No")
        app._on_selector_changed(SHORT, "state", "No", "Yes", {})
        self.assertEqual(saved(path)[-1]["short"], True)

    def test_quick_power_wash_has_the_option_too_and_other_programmes_have_none(self):
        app, path = make_app([record(FINISHED)], short="Yes")
        pick(app, "QuickPowerWash")
        self.assertEqual((saved(path)[-1]["confirmed"], saved(path)[-1]["short"]), ("quick", True))
        pick(app, "Gentle", old="QuickPowerWash")
        self.assertEqual(saved(path)[-1]["confirmed"], "gentle")
        self.assertNotIn("short", saved(path)[-1])

    def test_a_short_flip_changes_nothing_for_a_programme_without_the_option(self):
        app, path = make_app([record(FINISHED)], short="No")
        pick(app, "Intensive")
        before = Path(path).read_text()
        pick_short(app, "Yes")
        pick_short(app, "No", old="Yes")
        self.assertEqual(Path(path).read_text(), before)

    def test_short_left_unset_falls_back_to_the_classifiers_guess(self):
        app, path = make_app([record(FINISHED, short=True)], short="—")
        app.detected_short = True
        pick(app, "ECO")
        self.assertEqual(saved(path)[-1]["short"], True)
        app.detected_short = False
        pick(app, "ECO")
        self.assertEqual(saved(path)[-1]["short"], False)

    def test_the_same_pair_twice_writes_nothing_more(self):
        app, path = make_app([record(FINISHED)], short="No")
        pick(app, "ECO")
        before = Path(path).read_text()
        pick(app, "ECO")
        pick_short(app, "No", old="No")
        self.assertEqual(Path(path).read_text(), before)

    def test_a_short_change_with_no_programme_picked_leaves_the_guess_alone(self):
        app, path = make_app([record(FINISHED)], programme="—")
        pick_short(app, "Yes")
        self.assertEqual((saved(path)[-1]["short"], saved(path)[-1]["programme_confirmed_by_human"]), (False, False))

    def test_a_short_change_while_the_run_is_going_or_closed_is_not_applied(self):
        for state in ("Running", "Off"):
            app, path = make_app([record(FINISHED)], state=state, programme="ECO")
            pick_short(app, "Yes")
            self.assertFalse(saved(path)[-1]["short"], state)


class ListenerTests(unittest.TestCase):
    def test_initialize_listens_to_both_selectors_when_they_are_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extra, expected in (
                ({"confirmed_programme_entity": SELECTOR, "short_entity": SHORT}, [SELECTOR, SHORT]),
                ({"confirmed_programme_entity": SELECTOR}, [SELECTOR]),
                ({}, []),
            ):
                app, _ = audit.make_boot_app(tmp, extra_args=extra)
                listened = []
                app.listen_state = lambda callback, entity, **kw: listened.append((callback.__name__, entity))
                app.initialize()
                picked = [entity for name, entity in listened if name == "_on_selector_changed"]
                self.assertEqual(picked, expected)


if __name__ == "__main__":
    unittest.main()
