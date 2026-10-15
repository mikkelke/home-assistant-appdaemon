# tests/test_dishwasher_pick_after_finish.py - a programme picked on input_select.dishwasher_confirmed_programme
# once the run has finished confirms that run's feedback record, as the washer's picker does for its last
# one: the hygiene run (Intensive) is logged that way, and HouseUpcoming's row counts from it.
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
SELECTOR = "input_select.dishwasher_confirmed_programme"
SHORT = "input_select.dishwasher_short"


def record(ts, confirmed="eco", human=False, **extra):
    rec = {"ts": ts, "duration_min": 229.2, "energy_kwh": 0.71, "predicted": "eco", "confirmed": confirmed,
           "programme_confirmed_by_human": human, "max_power_w": 1946.0}
    if confirmed == "eco":
        rec["short"] = False
    rec.update(extra)
    return rec


def make_app(cycles, state="Unemptied", short="No"):
    tmp = tempfile.mkdtemp(prefix="dishwasher_pick_")
    path = os.path.join(tmp, "dishwasher_feedback.json")
    with open(path, "w") as f:
        json.dump({"version": 2, "cycles": cycles}, f)
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
    app.feedback_file = path
    app.state_entity = "sensor.dishwasher_state"
    app.confirmed_programme_entity = SELECTOR
    app.short_entity = SHORT
    app._learned_durations = {}
    app.detected_short = False
    app.detected_quick_short = False
    app.logs = []
    app.log = lambda msg, level="INFO", **kw: app.logs.append((level, msg))
    app.states = {app.state_entity: state, SHORT: short}
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app._now_utc = lambda: NOW
    return app, path


def saved(path):
    with open(path) as f:
        return json.load(f)["cycles"]


def pick(app, label, old="—"):
    app.states[SELECTOR] = label
    app._on_programme_picked(SELECTOR, "state", old, label, {})


class PickAfterFinishTests(unittest.TestCase):
    FINISHED = (NOW - timedelta(hours=3)).isoformat()

    def test_intensive_picked_while_unemptied_confirms_the_last_run_as_the_hygiene_run(self):
        app, path = make_app([record("2026-10-10T02:00:00+00:00"), record(self.FINISHED)])
        pick(app, "Intensive")
        first, last = saved(path)
        self.assertEqual((first["confirmed"], first["programme_confirmed_by_human"]), ("eco", False))
        self.assertEqual((last["confirmed"], last["programme_confirmed_by_human"]), ("intensive", True))
        self.assertEqual(last["programme_confirmed_via"], "picked_after_finish")
        self.assertNotIn("short", last)
        self.assertEqual((last["ts"], last["duration_min"], last["energy_kwh"]), (self.FINISHED, 229.2, 0.71))

    def test_the_learned_durations_follow_the_file(self):
        app, path = make_app([record(self.FINISHED)])
        pick(app, "Intensive")
        self.assertEqual(app._learned_durations, {"intensive": {"n": 1, "avg": 229.2}})
        fresh, _ = make_app([])
        fresh.feedback_file = path
        fresh._load_and_apply_feedback()
        self.assertEqual(fresh._learned_durations, app._learned_durations)

    def test_emptied_counts_like_unemptied(self):
        app, path = make_app([record(self.FINISHED)], state="Emptied")
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "intensive")

    def test_off_counts_only_while_the_record_is_recent(self):
        app, path = make_app([record((NOW - timedelta(hours=11)).isoformat())], state="Off")
        pick(app, "Intensive")
        self.assertEqual(saved(path)[-1]["confirmed"], "intensive")
        old, old_path = make_app([record((NOW - timedelta(hours=13)).isoformat())], state="Off")
        pick(old, "Intensive")
        self.assertEqual(saved(old_path)[-1]["confirmed"], "eco")

    def test_a_pick_while_the_run_is_going_is_left_to_the_finish(self):
        for state in ("Running", "Paused"):
            app, path = make_app([record(self.FINISHED)], state=state)
            pick(app, "Intensive")
            self.assertEqual(saved(path)[-1]["confirmed"], "eco", state)

    def test_nothing_to_confirm_with_no_programme_picked_or_no_record(self):
        for label in ("—", "Not set", "unknown", "unavailable", None):
            app, path = make_app([record(self.FINISHED)])
            pick(app, label)
            self.assertEqual(saved(path)[-1]["confirmed"], "eco", label)
        app, path = make_app([])
        pick(app, "Intensive")
        self.assertEqual(saved(path), [])

    def test_a_correction_replaces_the_pick_and_unmakes_the_hygiene_run(self):
        app, path = make_app([record(self.FINISHED)])
        pick(app, "Intensive")
        pick(app, "ECO", old="Intensive")
        last = saved(path)[-1]
        self.assertEqual((last["confirmed"], last["programme_confirmed_by_human"], last["short"]), ("eco", True, False))
        self.assertEqual(app._learned_durations, {"eco": {"n": 1, "avg": 229.2}})

    def test_eco_and_quick_keep_the_short_selector_of_the_moment(self):
        app, path = make_app([record(self.FINISHED)], short="Yes")
        pick(app, "ECO")
        self.assertEqual(saved(path)[-1]["short"], True)
        pick(app, "QuickPowerWash", old="ECO")
        self.assertEqual((saved(path)[-1]["confirmed"], saved(path)[-1]["short"]), ("quick", True))

    def test_the_same_pick_twice_writes_nothing_more(self):
        app, path = make_app([record(self.FINISHED)])
        pick(app, "Intensive")
        before = Path(path).read_text()
        pick(app, "Intensive")
        self.assertEqual(Path(path).read_text(), before)

    def test_an_unreadable_file_is_logged_and_left_alone(self):
        app, path = make_app([record(self.FINISHED)])
        Path(path).write_text("{not json")
        pick(app, "Intensive")
        self.assertEqual(Path(path).read_text(), "{not json")
        self.assertTrue(any(level == "WARNING" for level, _ in app.logs))
        os.remove(path)
        pick(app, "Intensive")

    def test_initialize_listens_to_the_selector_when_it_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            for extra, expected in (({"confirmed_programme_entity": SELECTOR}, [SELECTOR]), ({}, [])):
                app, _ = audit.make_boot_app(tmp, extra_args=extra)
                listened = []
                app.listen_state = lambda callback, entity, **kw: listened.append((callback.__name__, entity))
                app.initialize()
                picked = [entity for name, entity in listened if name == "_on_programme_picked"]
                self.assertEqual(picked, expected)


if __name__ == "__main__":
    unittest.main()
