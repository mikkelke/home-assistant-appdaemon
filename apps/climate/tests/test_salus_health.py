# tests/test_salus_health.py - SalusHealth watchdog (connectivity/problem/battery for
# the Salus thermostats + the Control Centre wiring centre). Same __new__ +
# monkeypatched-callables harness as the other apps/climate/tests files; async notify
# bodies are tested directly via IsolatedAsyncioTestCase, matching climate_alarm.py.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
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

import salus_health as sh  # noqa: E402

THERMOSTAT_CONN = "binary_sensor.bedroom_thermostat_connectivity"
THERMOSTAT_PROBLEM = "binary_sensor.bedroom_thermostat_problem"
THERMOSTAT_BATTERY = "sensor.bedroom_thermostat_battery_level"
CONTROLLER_CONN = "binary_sensor.control_centre_connectivity"
CONTROLLER_PROBLEM = "binary_sensor.control_centre_problem"


def _base_app():
    """SalusHealth instance without running AppDaemon's initialize() - knobs and
    per-entity state dicts set directly, matching the make_app() helpers in the
    sibling test files."""
    app = sh.SalusHealth.__new__(sh.SalusHealth)
    app.thermostat_marker = "thermostat"
    app.controller_marker = "control_centre"
    app.offline_minutes = 10
    app.battery_warn_level = 2
    app.notify_target = "mikkel"
    app.exclude_entities = set()
    app._offline_timer_handles = {}
    app._offline_alerted = {}
    app._problem_alerted = {}
    app._battery_alerted_level = {}
    app.log = MagicMock()
    app.create_task = MagicMock()
    app._save_state = MagicMock()
    app._notifier = MagicMock()
    return app


class FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, **kwargs):
        self.calls.append(kwargs)


class DiscoverEntities(unittest.TestCase):
    def _app(self, binary_sensors, sensors, exclude=None):
        app = _base_app()
        app.exclude_entities = set(exclude or [])

        def get_state(entity=None, **kw):
            if entity == "binary_sensor":
                return dict(binary_sensors)
            if entity == "sensor":
                return dict(sensors)
            return None

        app.get_state = get_state
        return app

    def test_finds_thermostat_and_controller_connectivity_and_problem(self):
        binary_sensors = {
            THERMOSTAT_CONN: "on",
            THERMOSTAT_PROBLEM: "off",
            CONTROLLER_CONN: "on",
            CONTROLLER_PROBLEM: "off",
            "binary_sensor.front_door_contact": "off",  # unrelated domain entity
            "binary_sensor.some_other_integration_problem": "off",  # no marker match
        }
        app = self._app(binary_sensors, {})
        connectivity, problem, battery = app._discover_entities()
        self.assertEqual(set(connectivity), {THERMOSTAT_CONN, CONTROLLER_CONN})
        self.assertEqual(set(problem), {THERMOSTAT_PROBLEM, CONTROLLER_PROBLEM})
        self.assertEqual(battery, [])

    def test_finds_thermostat_battery_but_ignores_non_battery_sensors(self):
        sensors = {
            THERMOSTAT_BATTERY: "4",
            "sensor.control_centre_rssi": "-27",  # wrong suffix
            "sensor.some_unrelated_thermostat_temperature": "21.0",  # wrong suffix
        }
        app = self._app({}, sensors)
        _, _, battery = app._discover_entities()
        self.assertEqual(set(battery), {THERMOSTAT_BATTERY})

    def test_exclude_entities_knob_drops_a_match(self):
        binary_sensors = {THERMOSTAT_CONN: "on", CONTROLLER_CONN: "on"}
        app = self._app(binary_sensors, {}, exclude=[THERMOSTAT_CONN])
        connectivity, _, _ = app._discover_entities()
        self.assertEqual(set(connectivity), {CONTROLLER_CONN})


class RoomLabelAndController(unittest.TestCase):
    def test_thermostat_label_strips_suffix_and_thermostat_word(self):
        app = _base_app()
        self.assertEqual(app._room_label(THERMOSTAT_CONN), "Bedroom")
        self.assertEqual(
            app._room_label("binary_sensor.claudias_room_thermostat_problem"),
            "Claudias Room",
        )
        self.assertEqual(app._room_label(THERMOSTAT_BATTERY), "Bedroom")

    def test_controller_label_is_control_centre(self):
        app = _base_app()
        self.assertEqual(app._room_label(CONTROLLER_CONN), "Control Centre")
        self.assertEqual(app._room_label(CONTROLLER_PROBLEM), "Control Centre")

    def test_is_controller(self):
        app = _base_app()
        self.assertTrue(app._is_controller(CONTROLLER_CONN))
        self.assertFalse(app._is_controller(THERMOSTAT_CONN))


class OfflineArmDisarm(unittest.TestCase):
    def _app(self):
        app = _base_app()
        self.run_in_calls = []

        def run_in(cb, delay, **kw):
            self.run_in_calls.append((cb, delay, kw))
            return f"handle-{len(self.run_in_calls)}"

        app.run_in = run_in
        app.timer_running = MagicMock(return_value=True)
        app.cancel_timer = MagicMock()
        return app

    def test_off_arms_a_timer_for_offline_minutes(self):
        app = self._app()
        app._on_connectivity_change(THERMOSTAT_CONN, "state", "on", "off", {})
        self.assertEqual(len(self.run_in_calls), 1)
        cb, delay, kw = self.run_in_calls[0]
        self.assertEqual(cb, app._maybe_alert_offline)
        self.assertEqual(delay, 600)  # 10 min default, in seconds
        self.assertEqual(kw.get("entity_id"), THERMOSTAT_CONN)
        self.assertIn(THERMOSTAT_CONN, app._offline_timer_handles)

    def test_blip_shorter_than_threshold_does_not_alert(self):
        app = self._app()
        app._on_connectivity_change(THERMOSTAT_CONN, "state", "on", "off", {})
        app._on_connectivity_change(THERMOSTAT_CONN, "state", "off", "on", {})
        app.cancel_timer.assert_called_once()
        self.assertNotIn(THERMOSTAT_CONN, app._offline_timer_handles)

        # Even if the (cancelled) timer's callback somehow still fires late, the
        # current state is "on" now, so _maybe_alert_offline must refuse to alert.
        app.get_state = lambda e, **kw: "on"
        app._maybe_alert_offline({"entity_id": THERMOSTAT_CONN})
        app.create_task.assert_not_called()

    def test_unknown_neither_arms_nor_disarms(self):
        app = self._app()
        app._on_connectivity_change(THERMOSTAT_CONN, "state", "on", "unknown", {})
        self.assertEqual(self.run_in_calls, [])
        app.cancel_timer.assert_not_called()

        app._offline_timer_handles[THERMOSTAT_CONN] = "handle-x"
        app._on_connectivity_change(THERMOSTAT_CONN, "state", "off", "unavailable", {})
        app.cancel_timer.assert_not_called()
        self.assertIn(THERMOSTAT_CONN, app._offline_timer_handles)


class OfflineAlertFiring(unittest.TestCase):
    def _app(self, state="off"):
        app = _base_app()
        app.get_state = lambda e, **kw: state
        return app

    def test_sustained_offline_fires_once(self):
        app = self._app("off")
        app._maybe_alert_offline({"entity_id": THERMOSTAT_CONN})
        app.create_task.assert_called_once()
        self.assertTrue(app._offline_alerted[THERMOSTAT_CONN])
        app._save_state.assert_called_once()

    def test_does_not_fire_twice_for_the_same_episode(self):
        app = self._app("off")
        app._maybe_alert_offline({"entity_id": THERMOSTAT_CONN})
        app._maybe_alert_offline({"entity_id": THERMOSTAT_CONN})
        app.create_task.assert_called_once()

    def test_no_alert_if_state_recovered_before_timer_fired(self):
        app = self._app("on")
        app._maybe_alert_offline({"entity_id": THERMOSTAT_CONN})
        app.create_task.assert_not_called()

    def test_missing_entity_id_in_kwargs_is_a_noop(self):
        app = self._app("off")
        app._maybe_alert_offline({})
        app.create_task.assert_not_called()


class OfflineRecovery(unittest.TestCase):
    def _app(self):
        app = _base_app()
        app.run_in = MagicMock(return_value="handle-1")
        app.timer_running = MagicMock(return_value=True)
        app.cancel_timer = MagicMock()
        return app

    def test_recovery_after_alert_notifies_and_clears_flag(self):
        app = self._app()
        app._offline_alerted[THERMOSTAT_CONN] = True
        app._on_connectivity_change(THERMOSTAT_CONN, "state", "off", "on", {})
        app.create_task.assert_called_once()
        self.assertFalse(app._offline_alerted[THERMOSTAT_CONN])
        app._save_state.assert_called_once()

    def test_recovery_without_prior_alert_does_not_notify(self):
        app = self._app()
        app._on_connectivity_change(THERMOSTAT_CONN, "state", "off", "on", {})
        app.create_task.assert_not_called()


class OfflineSurvivesRestart(unittest.TestCase):
    """_init_connectivity replays the same decision listen_state's callback would
    have made for whatever transition happened while AppDaemon was down/restarting,
    using the persisted _offline_alerted flag to avoid duplicate or lost alerts."""

    def _app(self, state, alerted_before):
        app = _base_app()
        app.get_state = lambda e, **kw: state
        app.run_in = MagicMock(return_value="handle-1")
        if alerted_before:
            app._offline_alerted[THERMOSTAT_CONN] = True
        return app

    def test_still_off_and_already_alerted_does_not_rearm_or_renotify(self):
        app = self._app("off", alerted_before=True)
        app._init_connectivity(THERMOSTAT_CONN)
        app.run_in.assert_not_called()
        app.create_task.assert_not_called()
        self.assertTrue(app._offline_alerted[THERMOSTAT_CONN])

    def test_still_off_and_not_yet_alerted_arms_a_fresh_timer(self):
        app = self._app("off", alerted_before=False)
        app._init_connectivity(THERMOSTAT_CONN)
        app.run_in.assert_called_once()
        app.create_task.assert_not_called()

    def test_recovered_while_restarting_sends_recovery_once(self):
        app = self._app("on", alerted_before=True)
        app._init_connectivity(THERMOSTAT_CONN)
        app.create_task.assert_called_once()
        self.assertFalse(app._offline_alerted[THERMOSTAT_CONN])
        app._save_state.assert_called_once()

    def test_on_and_never_alerted_does_nothing(self):
        app = self._app("on", alerted_before=False)
        app._init_connectivity(THERMOSTAT_CONN)
        app.create_task.assert_not_called()
        app.run_in.assert_not_called()

    def test_unknown_at_startup_does_nothing(self):
        app = self._app("unknown", alerted_before=False)
        app._init_connectivity(THERMOSTAT_CONN)
        app.create_task.assert_not_called()
        app.run_in.assert_not_called()


class OfflineNotifyWording(unittest.IsolatedAsyncioTestCase):
    def _app(self):
        app = _base_app()
        app._notifier = FakeNotifier()
        return app

    async def test_thermostat_offline_wording(self):
        app = self._app()
        await app._notify_offline(THERMOSTAT_CONN)
        self.assertEqual(len(app._notifier.calls), 1)
        call = app._notifier.calls[0]
        self.assertEqual(call["title"], "Thermostat offline")
        self.assertIn("Bedroom thermostat", call["message"])
        self.assertNotIn("ALL room heating", call["message"])
        self.assertEqual(call["target"], "mikkel")

    async def test_controller_offline_uses_all_heating_wording(self):
        app = self._app()
        await app._notify_offline(CONTROLLER_CONN)
        call = app._notifier.calls[0]
        self.assertEqual(call["title"], "Heating controller offline")
        self.assertIn("ALL room heating", call["message"])

    async def test_recovery_wording_thermostat_vs_controller(self):
        app = self._app()
        await app._notify_recovery(THERMOSTAT_CONN)
        await app._notify_recovery(CONTROLLER_CONN)
        titles = [c["title"] for c in app._notifier.calls]
        self.assertEqual(titles, ["Thermostat back online", "Heating controller back online"])


class ProblemAlerting(unittest.TestCase):
    def _app(self):
        return _base_app()

    def test_turning_on_alerts_once(self):
        app = self._app()
        app._on_problem_change(THERMOSTAT_PROBLEM, "state", "off", "on", {})
        app.create_task.assert_called_once()
        self.assertTrue(app._problem_alerted[THERMOSTAT_PROBLEM])

        # A duplicate "on" (e.g. a second Error* register flips while already on)
        # must not push again.
        app._on_problem_change(THERMOSTAT_PROBLEM, "state", "on", "on", {})
        app.create_task.assert_called_once()

    def test_turning_off_clears_silently(self):
        app = self._app()
        app._problem_alerted[THERMOSTAT_PROBLEM] = True
        app._on_problem_change(THERMOSTAT_PROBLEM, "state", "on", "off", {})
        self.assertFalse(app._problem_alerted[THERMOSTAT_PROBLEM])
        app.create_task.assert_not_called()

    def test_unknown_holds(self):
        app = self._app()
        app._problem_alerted[THERMOSTAT_PROBLEM] = True
        app._on_problem_change(THERMOSTAT_PROBLEM, "state", "on", "unknown", {})
        app.create_task.assert_not_called()
        self.assertTrue(app._problem_alerted[THERMOSTAT_PROBLEM])


class ProblemSurvivesRestart(unittest.TestCase):
    def _app(self, state, alerted_before):
        app = _base_app()
        app.get_state = lambda e, **kw: state
        if alerted_before:
            app._problem_alerted[THERMOSTAT_PROBLEM] = True
        return app

    def test_still_on_and_already_alerted_does_not_renotify(self):
        app = self._app("on", alerted_before=True)
        app._init_problem(THERMOSTAT_PROBLEM)
        app.create_task.assert_not_called()
        self.assertTrue(app._problem_alerted[THERMOSTAT_PROBLEM])

    def test_newly_on_at_startup_notifies(self):
        app = self._app("on", alerted_before=False)
        app._init_problem(THERMOSTAT_PROBLEM)
        app.create_task.assert_called_once()


class ProblemNotifyWording(unittest.IsolatedAsyncioTestCase):
    def _app(self, errors=None):
        app = _base_app()
        app._notifier = FakeNotifier()

        async def get_state(entity, **kw):
            return {"attributes": {"errors": errors if errors is not None else []}}

        app.get_state = get_state
        return app

    async def test_includes_active_error_keys(self):
        app = self._app(errors=["Error10", "Error26"])
        await app._notify_problem(THERMOSTAT_PROBLEM)
        call = app._notifier.calls[0]
        self.assertEqual(call["title"], "Thermostat fault")
        self.assertIn("Error10", call["message"])
        self.assertIn("Error26", call["message"])

    async def test_no_errors_attribute_omits_detail(self):
        app = self._app(errors=[])
        await app._notify_problem(THERMOSTAT_PROBLEM)
        call = app._notifier.calls[0]
        self.assertNotIn("(", call["message"])

    async def test_controller_problem_mentions_all_heating(self):
        app = self._app(errors=["Error12"])
        await app._notify_problem(CONTROLLER_PROBLEM)
        call = app._notifier.calls[0]
        self.assertEqual(call["title"], "Heating controller fault")
        self.assertIn("ALL room heating", call["message"])


class BatteryAlerting(unittest.TestCase):
    def _app(self):
        return _base_app()

    def test_fires_once_at_threshold(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "2")
        app.create_task.assert_called_once()
        self.assertEqual(app._battery_alerted_level[THERMOSTAT_BATTERY], 2)

    def test_staying_at_the_same_level_does_not_renag(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "2")
        app._check_battery(THERMOSTAT_BATTERY, "2")
        app.create_task.assert_called_once()

    def test_further_drop_alerts_again(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "2")
        app._check_battery(THERMOSTAT_BATTERY, "1")
        self.assertEqual(app.create_task.call_count, 2)
        self.assertEqual(app._battery_alerted_level[THERMOSTAT_BATTERY], 1)

    def test_above_threshold_never_alerts(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "5")
        app.create_task.assert_not_called()
        self.assertEqual(app._battery_alerted_level, {})

    def test_recovery_above_threshold_rearms_for_a_future_drop(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "2")  # first low -> alert
        app._check_battery(THERMOSTAT_BATTERY, "5")  # battery replaced
        app._check_battery(THERMOSTAT_BATTERY, "2")  # drops again later -> alert again
        self.assertEqual(app.create_task.call_count, 2)

    def test_unknown_unavailable_and_none_never_alert(self):
        app = self._app()
        for raw in ("unknown", "unavailable", None):
            app._check_battery(THERMOSTAT_BATTERY, raw)
        app.create_task.assert_not_called()
        self.assertEqual(app._battery_alerted_level, {})


class ParseBatteryLevel(unittest.TestCase):
    def test_numeric_strings_parse(self):
        self.assertEqual(sh.SalusHealth._parse_battery_level("4"), 4)
        self.assertEqual(sh.SalusHealth._parse_battery_level("0"), 0)

    def test_unreadable_values_are_none(self):
        self.assertIsNone(sh.SalusHealth._parse_battery_level(None))
        self.assertIsNone(sh.SalusHealth._parse_battery_level("unknown"))
        self.assertIsNone(sh.SalusHealth._parse_battery_level("unavailable"))
        self.assertIsNone(sh.SalusHealth._parse_battery_level("garbage"))


class StatePersistence(unittest.TestCase):
    def _app(self, path):
        app = sh.SalusHealth.__new__(sh.SalusHealth)
        app.state_file = path
        app.log = MagicMock()
        return app

    def test_missing_file_loads_empty_dict(self):
        app = self._app("/nonexistent/dir/salus_health_state.json")
        self.assertEqual(app._load_state(), {})

    def test_save_then_load_round_trips_all_three_dicts(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._offline_alerted = {THERMOSTAT_CONN: True}
        app._problem_alerted = {THERMOSTAT_PROBLEM: False}
        app._battery_alerted_level = {THERMOSTAT_BATTERY: 1}
        app._save_state()

        reloaded = self._app(path)._load_state()
        self.assertEqual(reloaded["offline_alerted"], {THERMOSTAT_CONN: True})
        self.assertEqual(reloaded["problem_alerted"], {THERMOSTAT_PROBLEM: False})
        self.assertEqual(reloaded["battery_alerted_level"], {THERMOSTAT_BATTERY: 1})

    def test_save_leaves_no_tmp_file_behind(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._offline_alerted = {}
        app._problem_alerted = {}
        app._battery_alerted_level = {}
        app._save_state()
        self.assertFalse(os.path.exists(path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
