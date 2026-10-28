from __future__ import annotations

import copy
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import yaml

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

import bathroom_boost_trial as trial  # noqa: E402

TZ = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 10, 27, 2, 30, tzinfo=TZ)
THERMOSTAT = "climate.bathroom_thermostat"
FLOOR = "sensor.bathroom_floor_thermometer_temperature"
AIR = "sensor.bathroom_feel"
SEASON = "input_boolean.heating_season"
APP_DIR = Path(__file__).resolve().parents[1]


class Harness:
    def __init__(self, testcase, now=NOW, **overrides):
        directory = tempfile.TemporaryDirectory(prefix="bathroom-trial-test-")
        testcase.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.now = now
        self.args = dict(
            yaml.safe_load((APP_DIR / "bathroom_boost_trial.yaml").read_text())[
                "BathroomBoostTrial"
            ]
        )
        self.args["log_path"] = str(self.directory / "bathroom_boost_trial.jsonl")
        self.args.update(overrides)
        self.entities = {}
        self.set(FLOOR, 21)
        self.set(AIR, 20)
        self.set(SEASON, "on")
        self.set(THERMOSTAT, "heat", {"temperature": 19, "hvac_action": "idle"})
        self.app = self.initialize()

    def initialize(self):
        app = trial.BathroomBoostTrial.__new__(trial.BathroomBoostTrial)
        app.args = self.args
        app.log = MagicMock()
        app.get_now = lambda: self.now
        app.get_state = self.get_state
        app.call_service = MagicMock()
        app.listen_state = MagicMock()
        app.run_daily = MagicMock()
        app.run_every = MagicMock()
        app.initialize()
        self.app = app
        return app

    def set(self, entity, state, attributes=None, updated=None):
        self.entities[entity] = {
            "entity_id": entity,
            "state": str(state),
            "attributes": attributes or {},
            "last_updated": (updated or self.now).isoformat(),
            "last_changed": (updated or self.now).isoformat(),
        }

    def get_state(self, entity, attribute=None):
        state = self.entities.get(entity)
        if state is None:
            return None
        if attribute == "all":
            return copy.deepcopy(state)
        if attribute is None:
            return state["state"]
        return state["attributes"].get(attribute)

    def change(self, entity, state, attributes=None, stale_store=False):
        previous = self.get_state(entity, attribute="all")
        self.set(entity, state, attributes)
        current = self.get_state(entity, attribute="all")
        if stale_store:
            self.entities[entity] = previous
        self.app._on_state(entity, "all", previous, current, {})

    def advance(self, **duration):
        self.now += timedelta(**duration)

    def morning(self):
        self.app._on_morning({})

    def tick(self):
        self.app._on_tick({})

    def action(self, action, stale_store=False):
        self.change(THERMOSTAT, "heat", {"temperature": 22, "hvac_action": action}, stale_store)

    def rows(self, kind=None):
        path = Path(self.args["log_path"])
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if kind is None or row["kind"] == kind]

    def saved(self):
        return json.loads(self.app.state_file.read_text())["active"]


class Configuration(unittest.TestCase):
    def test_shipped_configuration_schedules_local_mornings_snapshots_and_minute_checks(self):
        h = Harness(self)
        self.assertEqual(h.app.thermostat, THERMOSTAT)
        self.assertEqual(h.app.floor, FLOOR)
        self.assertEqual(h.app.air, AIR)
        self.assertEqual(h.app.season, SEASON)
        self.assertEqual(
            [call.args[1].isoformat() for call in h.app.run_daily.call_args_list],
            ["02:30:00", "07:00:00", "08:30:00"],
        )
        self.assertEqual(h.app.run_every.call_args.args[2], 60)
        self.assertEqual(
            {call.args[1] for call in h.app.listen_state.call_args_list},
            {THERMOSTAT, FLOOR, AIR, SEASON},
        )
        self.assertTrue(
            all(call.kwargs == {"attribute": "all"} for call in h.app.listen_state.call_args_list)
        )
        h.app.call_service.assert_not_called()

    def test_invalid_configuration_logs_error_without_registering_callbacks(self):
        invalid = [
            ("thermostat", "sensor.thermostat"),
            ("floor", "sensor."),
            ("air", None),
            ("season", False),
            ("start", "25:00"),
            ("start", "07:00"),
            ("start", 230),
            ("latest_end", "02:30"),
            ("first_day", "not-a-date"),
            ("last_day", "2026-10-26"),
            ("min_boosts", -1),
            ("min_boosts", True),
            ("extend_until", "2026-11-15"),
            ("snapshot_times", "07:00"),
            ("snapshot_times", []),
            ("snapshot_times", ["bad"]),
            ("log_path", ""),
            ("boost_setpoint", 18),
            ("floor_rise", 0),
            ("max_heating_hours", -1),
            ("max_reading_age_hours", 0),
        ]
        for name in (
            "boost_setpoint",
            "rest_setpoint",
            "floor_below",
            "air_below",
            "floor_rise",
            "max_heating_hours",
            "max_reading_age_hours",
        ):
            invalid.extend((name, value) for value in ("nan", float("inf"), None, True))
        for name, value in invalid:
            with self.subTest(name=name, value=value):
                app = Harness(self, **{name: value}).app
                app.listen_state.assert_not_called()
                app.run_daily.assert_not_called()
                app.run_every.assert_not_called()
                app.call_service.assert_not_called()
                self.assertEqual(app.log.call_args.kwargs["level"], "ERROR")


class Mornings(unittest.TestCase):
    def test_all_twenty_one_days_alternate_from_first_day(self):
        for offset in range(21):
            with self.subTest(offset=offset):
                h = Harness(self, NOW + timedelta(days=offset))
                h.morning()
                row = h.rows("morning")[0]
                self.assertEqual(row["day_type"], "boost" if offset % 2 == 0 else "control")
                self.assertEqual(h.app.call_service.call_count, 1 if offset % 2 == 0 else 0)

    def test_boost_persists_ownership_before_request_and_sets_heat(self):
        h = Harness(self)
        persisted = []
        h.app.call_service.side_effect = lambda *args, **kwargs: persisted.append(h.saved())
        h.morning()
        h.app.call_service.assert_called_once_with(
            "climate/set_temperature", entity_id=THERMOSTAT, temperature=22.0, hvac_mode="heat"
        )
        self.assertEqual(
            persisted[0],
            {
                "start_ts": NOW.timestamp(),
                "start_floor": 21.0,
                "heating_seconds": 0.0,
                "heating_started_ts": None,
            },
        )
        self.assertEqual(h.rows("morning")[0]["floor"], 21.0)

    def test_control_morning_and_later_callbacks_never_write(self):
        h = Harness(self, NOW + timedelta(days=1))
        h.morning()
        h.action("heating")
        h.change(SEASON, "off")
        h.advance(hours=8)
        h.tick()
        h.app._on_snapshot({"snapshot_time": "08:30"})
        h.app.call_service.assert_not_called()
        self.assertEqual(len(h.rows("morning")), 1)
        self.assertFalse(h.app.state_file.exists())

    def test_warm_readings_and_season_off_skip(self):
        for entity, value, reason in (
            (FLOOR, 22, "floor_warm"),
            (AIR, 21.5, "air_warm"),
            (SEASON, "off", "season_off"),
        ):
            with self.subTest(reason=reason):
                h = Harness(self)
                h.set(entity, value)
                h.morning()
                h.app.call_service.assert_not_called()
                self.assertEqual(h.rows("skip")[0]["reason"], reason)
                self.assertEqual(len(h.rows("morning")), 0 if entity == SEASON else 1)

    def test_missing_nonfinite_and_unavailable_readings_skip(self):
        for entity, name in ((FLOOR, "floor"), (AIR, "air")):
            for value in (None, "unknown", "unavailable", "nan", "inf", "-inf"):
                with self.subTest(entity=entity, value=value):
                    h = Harness(self)
                    if value is None:
                        del h.entities[entity]
                    else:
                        h.set(entity, value)
                    h.morning()
                    h.app.call_service.assert_not_called()
                    self.assertEqual(h.rows("skip")[0]["reason"], name + "_unavailable")

    def test_stale_missing_invalid_and_future_last_updated_skip(self):
        stamps = [
            None,
            "bad",
            "2026-10-27T02:30:00",
            (NOW - timedelta(hours=24, seconds=1)).isoformat(),
            (NOW + timedelta(seconds=1)).isoformat(),
        ]
        for entity, name in ((FLOOR, "floor"), (AIR, "air")):
            for stamp in stamps:
                with self.subTest(entity=entity, stamp=stamp):
                    h = Harness(self)
                    h.entities[entity]["last_updated"] = stamp
                    h.morning()
                    h.app.call_service.assert_not_called()
                    self.assertEqual(h.rows("skip")[0]["reason"], name + "_stale")

    def test_age_limit_is_inclusive_and_uses_last_updated_not_last_changed(self):
        h = Harness(self)
        for entity in (FLOOR, AIR):
            h.entities[entity]["last_updated"] = (NOW - timedelta(hours=24)).isoformat()
            h.entities[entity]["last_changed"] = (NOW - timedelta(days=5)).isoformat()
        h.morning()
        self.assertEqual(h.app.call_service.call_count, 1)

    def test_daily_callback_cannot_start_twice_or_after_deadline(self):
        h = Harness(self)
        h.morning()
        h.change(FLOOR, 21.5)
        h.set(FLOOR, 21)
        h.morning()
        self.assertEqual(h.app.call_service.call_count, 2)
        late = Harness(self, NOW.replace(hour=6, minute=30))
        late.morning()
        late.app.call_service.assert_not_called()
        self.assertEqual(late.rows("skip")[0]["reason"], "outside_boost_window")

    def test_local_date_and_clock_are_used_with_a_utc_clock(self):
        h = Harness(self, NOW.astimezone(timezone.utc))
        h.morning()
        self.assertEqual(h.rows("morning")[0]["ts"], NOW.isoformat())
        self.assertEqual(h.app.call_service.call_count, 1)

    def test_initialization_never_adopts_a_manually_high_setpoint(self):
        h = Harness(self)
        h.set(THERMOSTAT, "heat", {"temperature": 22, "hvac_action": "heating"})
        h.advance(hours=1)
        h.initialize()
        h.tick()
        h.app.call_service.assert_not_called()
        self.assertIsNone(h.app._active)


class Stops(unittest.TestCase):
    def assert_stopped(self, h, reason):
        self.assertIsNone(h.app._active)
        self.assertIsNone(h.saved())
        h.app.call_service.assert_called_with(
            "climate/set_temperature", entity_id=THERMOSTAT, temperature=19.0
        )
        row = h.rows("stop")[-1]
        self.assertEqual(row["reason"], reason)
        self.assertEqual(row["day_type"], "boost")
        self.assertIn("floor", row)
        self.assertIn("air", row)
        self.assertIn("heating_hours", row)

    def test_floor_rise_stops_on_minute_check(self):
        h = Harness(self)
        h.morning()
        h.advance(minutes=1)
        h.set(FLOOR, 21.5)
        h.tick()
        self.assert_stopped(h, "floor_rise")

    def test_air_limit_stops_on_thermostat_change(self):
        h = Harness(self)
        h.morning()
        h.advance(minutes=1)
        h.set(AIR, 21.5)
        h.action("heating")
        self.assert_stopped(h, "air_warm")

    def test_season_off_callback_wins_over_stale_store(self):
        h = Harness(self)
        h.morning()
        h.advance(minutes=1)
        h.change(SEASON, "off", stale_store=True)
        self.assert_stopped(h, "season_off")
        self.assertEqual(h.entities[SEASON]["state"], "on")

    def test_latest_end_stops_even_when_sensors_are_unavailable(self):
        h = Harness(self)
        h.morning()
        h.set(FLOOR, "unavailable")
        h.set(AIR, "unknown")
        h.advance(hours=4)
        h.tick()
        self.assert_stopped(h, "latest_end")
        self.assertEqual(h.rows("stop")[0]["heating_hours"], 0)

    def test_heating_budget_excludes_idle_intervals_and_does_not_double_count_ticks(self):
        h = Harness(self)
        h.morning()
        h.advance(minutes=15)
        h.action("heating", stale_store=True)
        h.advance(minutes=15)
        h.tick()
        self.assertEqual(h.saved()["heating_seconds"], 900)
        h.advance(minutes=15)
        h.action("idle", stale_store=True)
        self.assertEqual(h.saved()["heating_seconds"], 1800)
        h.advance(minutes=30)
        h.tick()
        self.assertEqual(h.saved()["heating_seconds"], 1800)
        h.action("heating", stale_store=True)
        h.advance(minutes=89)
        h.tick()
        self.assertIsNotNone(h.app._active)
        h.advance(minutes=1)
        h.tick()
        self.assert_stopped(h, "max_heating_hours")
        self.assertEqual(h.rows("stop")[0]["heating_hours"], 2)

    def test_heating_already_active_at_start_counts_from_boost_start(self):
        h = Harness(self)
        h.set(THERMOSTAT, "heat", {"hvac_action": "heating"}, updated=NOW - timedelta(hours=1))
        h.morning()
        h.advance(hours=2)
        h.tick()
        self.assert_stopped(h, "max_heating_hours")
        self.assertEqual(h.rows("stop")[0]["heating_hours"], 2)

    def test_new_floor_callback_is_used_while_store_is_stale(self):
        h = Harness(self)
        h.morning()
        h.advance(minutes=1)
        h.change(FLOOR, 21.5, stale_store=True)
        self.assert_stopped(h, "floor_rise")
        self.assertEqual(h.rows("stop")[0]["floor"], 21.5)

    def test_newer_store_overrides_old_callback(self):
        h = Harness(self)
        h.morning()
        h.advance(minutes=1)
        h.action("heating")
        h.advance(minutes=1)
        h.set(THERMOSTAT, "heat", {"hvac_action": "idle"})
        h.tick()
        self.assertEqual(h.saved()["heating_seconds"], 60)
        self.assertIsNone(h.saved()["heating_started_ts"])

    def test_callbacks_after_stop_cannot_touch_thermostat(self):
        h = Harness(self)
        h.morning()
        h.change(FLOOR, 21.5)
        h.app.call_service.reset_mock()
        h.action("heating")
        h.change(SEASON, "off")
        h.advance(hours=10)
        h.tick()
        h.app.call_service.assert_not_called()


class Restarts(unittest.TestCase):
    def test_resume_preserves_active_heating_without_reissuing_boost(self):
        h = Harness(self)
        h.morning()
        h.action("heating")
        h.advance(minutes=30)
        h.tick()
        h.advance(minutes=30)
        h.initialize()
        h.app.call_service.assert_not_called()
        self.assertEqual(h.saved()["heating_seconds"], 3600)
        self.assertEqual(len(h.rows("morning")), 1)
        h.advance(hours=1)
        h.tick()
        self.assertEqual(h.rows("stop")[0]["reason"], "max_heating_hours")
        self.assertEqual(h.rows("stop")[0]["heating_hours"], 2)

    def test_idle_restart_does_not_accumulate_heating(self):
        h = Harness(self)
        h.morning()
        h.advance(hours=1)
        h.initialize()
        self.assertEqual(h.saved()["heating_seconds"], 0)
        self.assertIsNone(h.saved()["heating_started_ts"])
        h.app.call_service.assert_not_called()

    def test_restart_after_end_restores_using_original_boost_day(self):
        for duration in (timedelta(hours=4), timedelta(days=1), timedelta(days=22)):
            with self.subTest(duration=duration):
                h = Harness(self)
                h.morning()
                h.app._active["stop_reason"] = "floor_rise"
                h.app._save_state()
                h.now += duration
                h.initialize()
                h.app.call_service.assert_called_once_with(
                    "climate/set_temperature", entity_id=THERMOSTAT, temperature=19.0
                )
                row = h.rows("stop")[0]
                self.assertEqual(row["reason"], "restart_after_end")
                self.assertEqual(row["day"], "2026-10-27")
                self.assertEqual(row["day_type"], "boost")
                self.assertIsNone(h.saved())

    def test_restart_rechecks_season_floor_and_air_limits(self):
        for entity, value, reason in (
            (SEASON, "off", "season_off"),
            (FLOOR, 21.5, "floor_rise"),
            (AIR, 21.5, "air_warm"),
        ):
            with self.subTest(reason=reason):
                h = Harness(self)
                h.morning()
                h.advance(minutes=1)
                h.set(entity, value)
                h.initialize()
                self.assertEqual(h.rows("stop")[0]["reason"], reason)
                self.assertIsNone(h.saved())

    def test_corrupt_state_warns_and_never_adopts_thermostat(self):
        for raw in ("{", "[]", '{"active": []}', '{"active": {"start_ts": "nan"}}'):
            with self.subTest(raw=raw):
                h = Harness(self)
                h.app.state_file.write_text(raw)
                h.set(THERMOSTAT, "heat", {"temperature": 22, "hvac_action": "heating"})
                h.initialize()
                self.assertIsNone(h.app._active)
                h.app.call_service.assert_not_called()
                self.assertTrue(
                    any(call.kwargs.get("level") == "WARNING" for call in h.app.log.call_args_list)
                )


class LoggingAndFailures(unittest.TestCase):
    def test_snapshots_capture_both_day_types_and_continue_with_season_off(self):
        for day in (0, 1):
            h = Harness(self, NOW + timedelta(days=day))
            h.set(SEASON, "off")
            for hour, minute in ((7, 0), (8, 30)):
                h.now = h.now.replace(hour=hour, minute=minute)
                h.app._on_snapshot({"snapshot_time": f"{hour:02}:{minute:02}"})
            self.assertEqual(len(h.rows()), 2)
            self.assertTrue(all(row["kind"] == "snapshot" for row in h.rows()))
            self.assertTrue(
                all(row["day_type"] == ("boost" if day == 0 else "control") for row in h.rows())
            )
            self.assertTrue(all(row["floor"] == 21 and row["air"] == 20 for row in h.rows()))
            h.app.call_service.assert_not_called()

    def test_outside_trial_no_rows_or_writes_and_finished_is_logged_once(self):
        for offset in (-1, 21):
            h = Harness(self, NOW + timedelta(days=offset), min_boosts=0)
            for _ in range(3):
                h.morning()
                h.tick()
                h.app._on_snapshot({})
            h.app.call_service.assert_not_called()
            self.assertEqual(h.rows(), [])
            finished = [call for call in h.app.log.call_args_list if "finished" in call.args[0]]
            self.assertEqual(len(finished), 1 if offset > 0 else 0)
            if offset > 0:
                h.app.run_daily.assert_not_called()
                h.app.run_every.assert_not_called()

    def test_trial_finishes_when_running_across_last_day(self):
        h = Harness(self, NOW + timedelta(days=20), min_boosts=0)
        h.morning()
        h.change(FLOOR, 21.5)
        h.app.call_service.reset_mock()
        h.advance(days=1)
        h.tick()
        h.morning()
        h.app._on_snapshot({})
        h.app.call_service.assert_not_called()
        self.assertEqual(len(h.rows()), 2)
        self.assertTrue(
            any(call.args[0] == "BathroomBoostTrial: finished" for call in h.app.log.call_args_list)
        )

    def test_too_few_boosts_extend_the_trial_until_extend_until(self):
        h = Harness(self, NOW + timedelta(days=22))
        h.morning()
        self.assertEqual(h.rows("morning")[0]["day_type"], "boost")
        self.assertEqual(h.app.call_service.call_count, 1)
        h = Harness(self, datetime(2026, 12, 15, 2, 30, tzinfo=TZ))
        h.morning()
        self.assertEqual(h.rows(), [])
        h.app.call_service.assert_not_called()

    def test_enough_boosts_end_the_trial_on_last_day(self):
        h = Harness(self, NOW + timedelta(days=22))
        with Path(h.args["log_path"]).open("w") as stream:
            for _ in range(5):
                stream.write(json.dumps({"kind": "stop"}) + "\n")
        h.morning()
        h.app.call_service.assert_not_called()
        self.assertEqual(len(h.rows()), 5)

    def test_state_save_failure_prevents_boost(self):
        h = Harness(self)
        with patch.object(trial.os, "replace", side_effect=OSError("disk full")):
            h.morning()
        h.app.call_service.assert_not_called()
        self.assertIsNone(h.app._active)
        self.assertEqual(h.rows("skip")[0]["reason"], "state_save_failed")

    def test_failed_rest_keeps_persisted_ownership_and_retries(self):
        h = Harness(self)
        h.morning()
        h.app.call_service.side_effect = RuntimeError("Home Assistant offline")
        h.change(FLOOR, 21.5)
        self.assertIsNotNone(h.app._active)
        self.assertEqual(h.saved()["stop_reason"], "floor_rise")
        self.assertEqual(h.rows("stop"), [])
        h.app.call_service.side_effect = None
        h.set(FLOOR, 21)
        h.tick()
        self.assertIsNone(h.saved())
        self.assertEqual(h.rows("stop")[0]["reason"], "floor_rise")

    def test_failed_boost_request_restores_from_persisted_ownership(self):
        h = Harness(self)
        h.app.call_service.side_effect = [RuntimeError("timeout"), None]
        h.morning()
        self.assertEqual(h.app.call_service.call_count, 2)
        self.assertEqual(h.rows("stop")[0]["reason"], "boost_request_failed")
        self.assertIsNone(h.saved())

    def test_failed_stop_log_does_not_keep_controlling_thermostat(self):
        h = Harness(self)
        h.morning()
        with patch.object(h.app, "_append_log", return_value=False):
            h.change(FLOOR, 21.5)
        self.assertIsNone(h.saved())
        h.app.call_service.reset_mock()
        h.set(THERMOSTAT, "heat", {"temperature": 20, "hvac_action": "idle"})
        h.tick()
        h.app.call_service.assert_not_called()
        self.assertEqual(len(h.rows("stop")), 0)

    def test_failed_state_clear_retries_persistence_without_controlling_thermostat(self):
        h = Harness(self)
        h.morning()
        replace = trial.os.replace
        attempts = []

        def fail_clear(source, destination):
            attempts.append(json.loads(Path(source).read_text()))
            if attempts[-1]["active"] is None:
                raise OSError("disk full")
            replace(source, destination)

        with patch.object(trial.os, "replace", side_effect=fail_clear):
            h.change(FLOOR, 21.5)
        self.assertIsNone(h.app._active)
        self.assertIsNotNone(h.saved())
        h.app.call_service.reset_mock()
        h.tick()
        self.assertIsNone(h.saved())
        h.app.call_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
