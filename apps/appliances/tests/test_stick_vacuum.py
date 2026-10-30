# tests/test_stick_vacuum.py - session timing, per-mode suction, cleaner visit receipts,
# recorder replay and maintenance facts from the LG stick vacuum's local readings.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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

import stick_vacuum as sv  # noqa: E402

T0 = datetime(2026, 10, 29, 10, tzinfo=timezone.utc).timestamp()
ENTITIES = {
    "status": "sensor.lg_stick_vacuum_status",
    "power_mode": "sensor.lg_stick_vacuum_power_mode",
    "battery": "sensor.lg_stick_vacuum_battery",
    "filter": "binary_sensor.lg_stick_vacuum_filter_needs_cleaning",
    "clogged": "binary_sensor.lg_stick_vacuum_air_path_clogged",
    "visitor": "sensor.untracked_visitor",
    "door": "binary_sensor.apartment_door_open",
}


def row(when, state, **attrs):
    return {"state": state, "last_changed": sv.stamp(when), "attributes": attrs}


def make_app(path, now=T0, history=None, states=None):
    app = sv.StickVacuum.__new__(sv.StickVacuum)
    app.args = {"feedback_file": str(path)}
    app.clock = now
    app.get_now = lambda: datetime.fromtimestamp(app.clock, timezone.utc)
    defaults = {"status": "Ready", "power_mode": "Normal", "battery": "High", "filter": "off",
                "clogged": "off", "visitor": "none", "door": "off"}
    app.states = {ENTITIES[key]: row(now, value) for key, value in defaults.items()}
    app.states.update(states or {})
    app.get_state = lambda entity, **kwargs: app.states.get(entity)

    def get_history(**kwargs):
        if isinstance(history, Exception):
            raise history
        rows = (history or {}).get(kwargs["entity_id"], [])
        start, end = kwargs["start_time"].timestamp(), kwargs["end_time"].timestamp()
        before = [full for full in rows if sv.epoch(full["last_changed"]) <= start]
        after = [full for full in rows if start < sv.epoch(full["last_changed"]) <= end]
        return [(before[-1:] + after)]

    app.get_history = MagicMock(side_effect=get_history)
    app.set_state = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    app.log = MagicMock()
    app.fire_event = MagicMock()
    app.initialize()
    return app


def feed(app, seconds, key, value, **attrs):
    app.clock = T0 + seconds
    full = row(app.clock, value, **attrs)
    entity = ENTITIES[key]
    old = app.states.get(entity)
    app.states[entity] = full
    app._on_source(entity, "all", old, full, {"source": key})


def tick(app, seconds):
    app.clock = T0 + seconds
    app._tick({})


def published(app, entity=None):
    entity = entity or app.entity
    return [call.kwargs for call in app.set_state.call_args_list if call.args[0] == entity][-1]


class Vacuum(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "stick_vacuum_feedback.json"
        self.app = make_app(self.path)

    def test_ready_outside_a_session_is_off_mount(self):
        self.assertEqual(published(self.app)["state"], "off_mount")
        self.assertEqual(published(self.app, self.app.in_use_entity)["state"], "off")
        self.assertEqual(published(self.app)["attributes"]["coverage"], "unknown")

    def test_cleaning_opens_and_ready_keeps_a_session_until_twenty_minutes(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "Ready")
        tick(self.app, 1259)
        self.assertEqual(published(self.app)["state"], "in_use")
        self.assertEqual(published(self.app, self.app.in_use_entity)["state"], "on")
        tick(self.app, 1260)
        self.assertIsNone(self.app._session)
        self.assertEqual(published(self.app)["state"], "off_mount")
        record = self.app._data["cycles"][0]
        self.assertEqual(record["duration_min"], 1)
        self.assertEqual(record["end"], sv.stamp(T0 + 1260))

    def test_mount_closes_after_five_continuous_minutes(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "Charging")
        self.assertEqual(published(self.app, self.app.in_use_entity)["state"], "off")
        self.assertEqual(published(self.app)["attributes"]["session_open"], "yes")
        feed(self.app, 120, "status", "Fully charged")
        tick(self.app, 359)
        self.assertIsNotNone(self.app._session)
        tick(self.app, 360)
        self.assertIsNone(self.app._session)
        self.assertEqual(self.app._data["cycles"][0]["docked_at"], sv.stamp(T0 + 60))
        self.assertEqual(published(self.app)["state"], "charged")

    def test_a_short_mount_does_not_close_the_session_and_new_cleaning_resets_dock(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "Charging")
        feed(self.app, 180, "status", "Cleaning")
        self.assertIsNone(self.app._session["docked_at"])
        feed(self.app, 240, "status", "Charging")
        tick(self.app, 540)
        record = self.app._data["cycles"][0]
        self.assertEqual(record["docked_at"], sv.stamp(T0 + 240))
        self.assertEqual(record["trigger_starts"], 2)
        self.assertEqual(record["duration_min"], 2)

    def test_suction_is_bucketed_at_each_power_step_and_stops_when_ready(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 30, "power_mode", "High")
        feed(self.app, 50, "power_mode", "Turbo")
        feed(self.app, 60, "status", "Ready")
        feed(self.app, 120, "status", "Cleaning")
        feed(self.app, 140, "status", "Ready")
        tick(self.app, 1340)
        record = self.app._data["cycles"][0]
        self.assertEqual(record["suction_seconds"], {"Normal": 30, "High": 20, "Turbo": 30, "unknown": 0})
        self.assertEqual(record["trigger_starts"], 2)
        self.assertAlmostEqual(record["duration_min"], 80 / 60)

    def test_unavailable_suspends_suction_and_is_partial_without_inventing_a_trigger(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "unavailable")
        tick(self.app, 3600)
        self.assertIsNotNone(self.app._session)
        self.assertEqual(published(self.app)["state"], "unknown")
        self.assertEqual(published(self.app, self.app.in_use_entity)["state"], "unknown")
        feed(self.app, 3700, "status", "Cleaning")
        feed(self.app, 3760, "status", "Ready")
        tick(self.app, 4960)
        record = self.app._data["cycles"][0]
        self.assertEqual(record["coverage"], "partial")
        self.assertEqual(record["suction_seconds"]["Normal"], 120)
        self.assertEqual(record["trigger_starts"], 1)

    def test_ready_after_a_gap_then_cleaning_counts_a_second_trigger(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 20, "status", "unavailable")
        feed(self.app, 40, "status", "Ready")
        feed(self.app, 60, "status", "Cleaning")
        self.assertEqual(self.app._session["trigger_starts"], 2)

    def test_unknown_interrupts_mount_grace(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "Charging")
        feed(self.app, 120, "status", "unavailable")
        feed(self.app, 360, "status", "Charging")
        tick(self.app, 659)
        self.assertIsNotNone(self.app._session)
        tick(self.app, 660)
        self.assertIsNone(self.app._session)

    def test_session_belongs_to_cleaner_if_she_arrives_mid_session(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "visitor", "cleaner", since=sv.stamp(T0 + 60))
        feed(self.app, 120, "status", "Charging")
        tick(self.app, 420)
        self.assertEqual(self.app._data["cycles"][0]["visit"], "cleaner")

    def test_household_session_is_attributed_without_a_cleaner_visit(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "Ready")
        tick(self.app, 1260)
        self.assertEqual(self.app._data["cycles"][0]["visit"], "household")

    def test_visit_totals_are_clipped_to_cleaner_presence_and_record_door_and_dock(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "visitor", "cleaner", since=sv.stamp(T0 + 60), who="Liga Cleaning")
        feed(self.app, 120, "power_mode", "High")
        feed(self.app, 150, "status", "Ready")
        feed(self.app, 180, "status", "Cleaning")
        feed(self.app, 210, "status", "Charging")
        feed(self.app, 230, "door", "on")
        feed(self.app, 240, "door", "off")
        feed(self.app, 270, "visitor", "none", end_reason="arrival")
        record = self.app._data["visits"][0]
        self.assertEqual(record["start"], sv.stamp(T0 + 60))
        self.assertEqual(record["ts"], sv.stamp(T0 + 270))
        self.assertEqual(record["end"], record["ts"])
        self.assertEqual(record["end_reason"], "arrival")
        self.assertEqual(record["sessions"], [sv.stamp(T0)])
        self.assertEqual(record["suction_seconds"]["Normal"], 60)
        self.assertEqual(record["suction_seconds"]["High"], 60)
        self.assertEqual(record["duration_min"], 2)
        self.assertEqual(record["trigger_starts"], 1)
        self.assertEqual(record["docked_at"], sv.stamp(T0 + 210))
        self.assertEqual(record["door_closed_at"], sv.stamp(T0 + 240))
        event = self.app.fire_event.call_args
        self.assertEqual(event.args, ("house_events_report",))
        self.assertEqual(event.kwargs["audience"], "admin")
        self.assertEqual(event.kwargs["icon"], "mdi:vacuum")
        self.assertEqual(event.kwargs["text"], "Vacuumed 2 min: Normal 1, High 1, 1 starts")
        self.assertEqual(event.kwargs["by"], "Liga Cleaning")

    def test_receipt_text_contains_modes_starts_and_partial(self):
        record = {"duration_min": 42, "suction_seconds": {"Normal": 1800, "High": 540, "Turbo": 180},
                  "trigger_starts": 18, "coverage": "partial"}
        self.assertEqual(sv.receipt(record), "Vacuumed 42 min: Normal 30, High 9, Turbo 3, 18 starts (partial)")

    def test_receipt_failure_leaves_a_saved_visit_and_finishes_publishing(self):
        feed(self.app, 0, "visitor", "cleaner", since=sv.stamp(T0))
        feed(self.app, 10, "status", "Cleaning")
        feed(self.app, 70, "status", "Ready")
        self.app.fire_event.side_effect = RuntimeError("unavailable")
        feed(self.app, 100, "visitor", "none", end_reason="quiet")
        self.assertIsNone(self.app._visit)
        self.assertEqual(len(json.loads(self.path.read_text())["visits"]), 1)
        self.assertEqual(published(self.app)["attributes"]["last_visit"],
                         "Vacuumed 1 min: Normal 1, 1 starts")

    def test_no_use_still_records_visit_but_skips_receipt(self):
        feed(self.app, 0, "visitor", "cleaner", since=sv.stamp(T0))
        feed(self.app, 60, "visitor", "none", end_reason="quiet")
        self.assertEqual(len(self.app._data["visits"]), 1)
        self.assertEqual(self.app._data["visits"][0]["sessions"], [])
        self.app.fire_event.assert_not_called()

    def test_visits_can_end_before_session_closes_and_do_not_count_later_suction(self):
        feed(self.app, 0, "visitor", "cleaner", since=sv.stamp(T0))
        feed(self.app, 10, "status", "Cleaning")
        feed(self.app, 70, "visitor", "none", end_reason="cap")
        feed(self.app, 130, "status", "Ready")
        tick(self.app, 1330)
        self.assertEqual(self.app._data["visits"][0]["duration_min"], 1)
        self.assertEqual(self.app._data["cycles"][0]["duration_min"], 2)

    def test_visit_coverage_is_partial_for_an_unavailable_gap_inside_session(self):
        feed(self.app, 0, "visitor", "cleaner", since=sv.stamp(T0))
        feed(self.app, 10, "status", "Cleaning")
        feed(self.app, 70, "status", "unavailable")
        feed(self.app, 80, "status", "Ready")
        feed(self.app, 90, "visitor", "none", end_reason="quiet")
        self.assertEqual(self.app._data["visits"][0]["coverage"], "partial")
        self.assertTrue(self.app.fire_event.call_args.kwargs["text"].endswith("(partial)"))

    def test_clog_requires_two_minutes_and_filter_is_immediate(self):
        feed(self.app, 0, "clogged", "on")
        feed(self.app, 10, "filter", "on")
        tick(self.app, 119)
        self.assertEqual(published(self.app)["attributes"]["problems"], "filter")
        tick(self.app, 120)
        self.assertEqual(published(self.app)["attributes"]["problems"], "clogged,filter")
        feed(self.app, 130, "clogged", "off")
        feed(self.app, 140, "filter", "off")
        self.assertEqual(published(self.app)["attributes"]["problems"], "")

    def test_quiet_departure_not_charging_opens_after_thirty_minutes_and_clears_on_mount(self):
        feed(self.app, 0, "visitor", "cleaner", since=sv.stamp(T0))
        feed(self.app, 10, "status", "Cleaning")
        feed(self.app, 70, "status", "Ready")
        feed(self.app, 100, "visitor", "none", end_reason="quiet")
        tick(self.app, 1899)
        self.assertNotIn("not_charging", published(self.app)["attributes"]["problems"])
        tick(self.app, 1900)
        attrs = published(self.app)["attributes"]
        self.assertIn("not_charging", attrs["problems"])
        self.assertEqual(attrs["not_charging_push"], "yes")
        self.assertEqual(attrs["not_charging_since"], sv.stamp(T0 + 100))
        feed(self.app, 1910, "status", "Charging")
        feed(self.app, 1920, "status", "Ready")
        tick(self.app, 4000)
        self.assertNotIn("not_charging", published(self.app)["attributes"]["problems"])

    def test_cap_and_arrival_departures_are_row_only(self):
        for why in ("cap", "arrival", "hard_cap"):
            with self.subTest(why=why):
                app = make_app(Path(self.tmp.name) / (why + ".json"))
                feed(app, 0, "visitor", "cleaner", since=sv.stamp(T0))
                feed(app, 10, "status", "Cleaning")
                feed(app, 70, "status", "Ready")
                feed(app, 100, "visitor", "none", end_reason=why)
                tick(app, 1900)
                self.assertEqual(published(app)["attributes"]["not_charging_push"], "no")
                self.assertIn("not_charging", published(app)["attributes"]["problems"])

    def test_a_household_session_with_no_cleaner_suction_has_no_receipt_or_fault(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "Ready")
        feed(self.app, 70, "visitor", "cleaner", since=sv.stamp(T0 + 70))
        feed(self.app, 80, "visitor", "none", end_reason="quiet")
        tick(self.app, 2000)
        self.assertEqual(self.app._data["visits"][0]["duration_min"], 0)
        self.app.fire_event.assert_not_called()
        self.assertNotIn("not_charging", published(self.app)["attributes"]["problems"])

    def test_all_published_attributes_are_strings(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 10, "battery", None)
        for call in self.app.set_state.call_args_list:
            self.assertTrue(all(isinstance(value, str) for value in call.kwargs["attributes"].values()))

    def test_feedback_is_version_two_with_cycles_visits_and_no_tmp_left(self):
        feed(self.app, 0, "status", "Cleaning")
        feed(self.app, 60, "status", "Charging")
        tick(self.app, 360)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 2)
        self.assertEqual(len(data["cycles"]), 1)
        self.assertIn("visits", data)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "stick_vacuum_feedback.json"

    def test_replay_recovers_open_session_modes_and_partial_gap(self):
        history = {
            ENTITIES["power_mode"]: [row(T0, "Normal"), row(T0 + 30, "High")],
            ENTITIES["status"]: [row(T0, "Cleaning"), row(T0 + 60, "unavailable"), row(T0 + 90, "Ready")],
        }
        states = {ENTITIES["status"]: row(T0 + 90, "Ready"), ENTITIES["power_mode"]: row(T0 + 30, "High")}
        app = make_app(self.path, now=T0 + 100, history=history, states=states)
        self.assertEqual(app._session["start"], T0)
        self.assertEqual(app._session["suction_seconds"]["Normal"], 30)
        self.assertEqual(app._session["suction_seconds"]["High"], 30)
        self.assertEqual(app._session["coverage"], "partial")
        tick(app, 1290)
        self.assertEqual(len(app._data["cycles"]), 1)

    def test_replaying_closed_session_and_visit_never_duplicates_ledger_or_receipt(self):
        history = {
            ENTITIES["visitor"]: [row(T0, "cleaner", since=sv.stamp(T0)),
                                  row(T0 + 500, "none", end_reason="quiet")],
            ENTITIES["power_mode"]: [row(T0, "Normal")],
            ENTITIES["status"]: [row(T0 + 10, "Cleaning"), row(T0 + 70, "Charging")],
        }
        states = {ENTITIES["status"]: row(T0 + 70, "Charging"),
                  ENTITIES["visitor"]: row(T0 + 500, "none", end_reason="quiet")}
        app = make_app(self.path, now=T0 + 600, history=history, states=states)
        self.assertEqual(len(app._data["cycles"]), 1)
        self.assertEqual(len(app._data["visits"]), 1)
        app2 = make_app(self.path, now=T0 + 700, history=history, states=states)
        self.assertEqual(len(app2._data["cycles"]), 1)
        self.assertEqual(len(app2._data["visits"]), 1)
        app2.fire_event.assert_not_called()

    def test_active_visit_that_began_before_latest_cycle_recovers_earlier_suction(self):
        history = {
            ENTITIES["visitor"]: [row(T0, "cleaner", since=sv.stamp(T0))],
            ENTITIES["power_mode"]: [row(T0, "Normal")],
            ENTITIES["status"]: [row(T0 + 10, "Cleaning"), row(T0 + 70, "Charging"),
                                 row(T0 + 500, "Cleaning"), row(T0 + 620, "Ready")],
        }
        states = {ENTITIES["status"]: row(T0 + 620, "Ready"),
                  ENTITIES["visitor"]: row(T0, "cleaner", since=sv.stamp(T0))}
        first = make_app(self.path, now=T0 + 600, history=history,
                         states={**states, ENTITIES["status"]: row(T0 + 500, "Cleaning")})
        self.assertEqual(len(first._data["cycles"]), 1)
        app = make_app(self.path, now=T0 + 700, history=history, states=states)
        feed(app, 800, "visitor", "none", end_reason="quiet")
        visit = app._data["visits"][0]
        self.assertEqual(visit["duration_min"], 3)
        self.assertEqual(len(visit["sessions"]), 2)

    def test_docked_visit_does_not_resurrect_not_charging_after_restart(self):
        history = {
            ENTITIES["visitor"]: [row(T0, "cleaner", since=sv.stamp(T0)),
                                  row(T0 + 500, "none", end_reason="quiet")],
            ENTITIES["power_mode"]: [row(T0, "Normal")],
            ENTITIES["status"]: [row(T0 + 10, "Cleaning"), row(T0 + 70, "Charging"),
                                 row(T0 + 600, "Ready")],
        }
        states = {ENTITIES["status"]: row(T0 + 600, "Ready"),
                  ENTITIES["visitor"]: row(T0 + 500, "none", end_reason="quiet")}
        make_app(self.path, now=T0 + 700, history=history, states=states)
        restored = make_app(self.path, now=T0 + 3000, history=history, states=states)
        self.assertNotIn("not_charging", published(restored)["attributes"]["problems"])

    def test_clog_episode_onset_survives_replay_boundary_and_history_failure(self):
        history = {
            ENTITIES["clogged"]: [row(T0 - 600, "on")],
            ENTITIES["status"]: [row(T0, "Cleaning"), row(T0 + 60, "Charging")],
        }
        states = {ENTITIES["status"]: row(T0 + 60, "Charging"),
                  ENTITIES["clogged"]: row(T0 - 600, "on")}
        first = make_app(self.path, now=T0 + 400, history=history, states=states)
        original = published(first)["attributes"]["clogged_since"]
        self.assertEqual(original, sv.stamp(T0 - 600))
        restored = make_app(self.path, now=T0 + 500, history=history, states=states)
        self.assertEqual(published(restored)["attributes"]["clogged_since"], original)
        states[ENTITIES["clogged"]] = {"state": "on", "attributes": {}}
        failed = make_app(self.path, now=T0 + 600, history=RuntimeError("offline"), states=states)
        self.assertEqual(published(failed)["attributes"]["clogged_since"], original)
        self.assertIn("clogged", published(failed)["attributes"]["problems"])

    def test_semantic_since_is_recovered_from_history_and_exact_close_deadline(self):
        history = {ENTITIES["status"]: [row(T0, "Cleaning"), row(T0 + 60, "Ready")]}
        states = {ENTITIES["status"]: row(T0 + 60, "Ready")}
        app = make_app(self.path, now=T0 + 100, history=history, states=states)
        self.assertEqual(published(app)["attributes"]["since"], sv.stamp(T0))
        tick(app, 2000)
        self.assertEqual(published(app)["attributes"]["since"], sv.stamp(T0 + 1260))

    def test_empty_history_keeps_checkpoint_session_and_does_not_add_a_trigger(self):
        app = make_app(self.path)
        feed(app, 0, "status", "Cleaning")
        tick(app, 60)
        states = {ENTITIES["status"]: row(T0, "Cleaning")}
        restored = make_app(self.path, now=T0 + 1000, history={}, states=states)
        self.assertEqual(restored._session["start"], T0)
        self.assertEqual(restored._session["trigger_starts"], 1)
        self.assertEqual(restored._session["suction_seconds"]["Normal"], 60)
        self.assertEqual(restored._session["coverage"], "partial")

    def test_replay_lower_bound_never_truncates_a_previously_recorded_session(self):
        history = {
            ENTITIES["visitor"]: [row(T0, "cleaner", since=sv.stamp(T0)),
                                  row(T0 + 100, "none", end_reason="arrival")],
            ENTITIES["power_mode"]: [row(T0, "Normal")],
            ENTITIES["status"]: [row(T0 + 10, "Cleaning"), row(T0 + 150, "Charging"),
                                 row(T0 + 500, "Cleaning"), row(T0 + 560, "Charging")],
        }
        states = {ENTITIES["status"]: row(T0 + 560, "Charging"),
                  ENTITIES["visitor"]: row(T0 + 100, "none", end_reason="arrival")}
        first = make_app(self.path, now=T0 + 1000, history=history, states=states)
        self.assertEqual(len(first._data["cycles"]), 2)
        restored = make_app(self.path, now=T0 + 1100, history=history, states=states)
        self.assertEqual(len(restored._data["cycles"]), 2)
        self.assertEqual([record["start"] for record in restored._data["cycles"]],
                         [sv.stamp(T0 + 10), sv.stamp(T0 + 500)])

    def test_unreadable_history_uses_checkpoint_without_counting_gap_as_suction(self):
        app = make_app(self.path)
        feed(app, 0, "status", "Cleaning")
        tick(app, 60)
        states = {ENTITIES["status"]: row(T0 + 1000, "Ready")}
        restored = make_app(self.path, now=T0 + 1000, history=RuntimeError("offline"), states=states)
        self.assertEqual(restored._session["suction_seconds"]["Normal"], 60)
        self.assertEqual(restored._session["coverage"], "partial")
        self.assertEqual(restored._session["start"], T0)


if __name__ == "__main__":
    unittest.main()
