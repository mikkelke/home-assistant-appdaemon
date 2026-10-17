# tests/test_salus_health.py - SalusHealth watchdog (connectivity for the Salus thermostats +
# the wiring centre, and thermostat battery, all on zigbee2mqtt). Same __new__ +
# monkeypatched-callables harness as the other apps/climate/tests files; async notify
# bodies are tested directly via IsolatedAsyncioTestCase, matching climate_alarm.py.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import salus_health as sh  # noqa: E402

ROOMS = (
    "kristines_room",
    "claudias_room",
    "bedroom",
    "guest_bathroom",
    "bathroom",
    "family_room",
    "living_room",
)

THERMOSTAT = "climate.bedroom_thermostat"
LIVING_ROOM = "climate.living_room_thermostat"
AC = "climate.air_conditioner_thermostat"
CONTROLLER = "sensor.wiring_centre_zone_1"
THERMOSTAT_BATTERY = "sensor.bedroom_thermostat_battery_level"
ADDRESS = "0x26b0"  # a zone sensor's state while the wiring centre is online


def _house_climates():
    climates = {f"climate.{room}_thermostat": "off" for room in ROOMS}
    climates[AC] = "unavailable"
    return climates


def _house_sensors():
    sensors = {}
    for room in ROOMS:
        sensors[f"sensor.{room}_thermostat_battery_level"] = "100"
        sensors[f"sensor.{room}_thermostat_humidity"] = "45"
    for zone in range(1, 9):
        sensors[f"sensor.wiring_centre_zone_{zone}"] = ADDRESS
    return sensors


def _close_coroutine(coro):
    coro.close()


def _base_app():
    """SalusHealth instance without running AppDaemon's initialize() - knobs and
    per-entity state dicts set directly, matching the make_app() helpers in the
    sibling test files."""
    app = sh.SalusHealth.__new__(sh.SalusHealth)
    app.thermostat_marker = "thermostat"
    app.controller_entity = CONTROLLER
    app.offline_minutes = 10
    app.battery_warn_percent = 40
    app.notify_target = "mikkel"
    app.exclude_entities = set()
    app._offline_timer_handles = {}
    app._offline_alerted = {}
    app._battery_alerted_percent = {}
    app.log = MagicMock()
    app.create_task = MagicMock(side_effect=_close_coroutine)
    app._save_state = MagicMock()
    app._notifier = MagicMock()
    return app


def _initialized_app(args, *, climates=None, sensors=None):
    """SalusHealth with initialize() run for real against fake entity domains.
    Values are plain state strings. listen_state / run_in / create_task are mocks
    to assert on."""
    app = sh.SalusHealth.__new__(sh.SalusHealth)
    app.args = args
    app.log = MagicMock()
    app.get_app = lambda name: MagicMock()
    app.listen_state = MagicMock()
    app.run_in = MagicMock(return_value="handle-1")
    app.create_task = MagicMock(side_effect=_close_coroutine)

    domains = {
        "climate": climates or {},
        "sensor": sensors or {},
    }

    def get_state(entity=None, **kw):
        if entity in domains:
            return dict(domains[entity])
        for states in domains.values():
            if entity in states:
                return states[entity]
        return None

    app.get_state = get_state
    app.initialize()
    return app


def _write_state_file(testcase, content):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(content, f)
    testcase.addCleanup(lambda: os.path.exists(path) and os.remove(path))
    return path


class FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify(self, **kwargs):
        self.calls.append(kwargs)


class DiscoverEntities(unittest.TestCase):
    def _app(self, climates=None, sensors=None, exclude=None):
        app = _base_app()
        app.exclude_entities = set(exclude or [])
        domains = {
            "climate": climates or {},
            "sensor": sensors or {},
        }

        def get_state(entity=None, **kw):
            return dict(domains[entity]) if entity in domains else None

        app.get_state = get_state
        return app

    def test_finds_thermostat_climates_by_marker(self):
        climates = {
            THERMOSTAT: "heat",
            "climate.claudias_room_thermostat": "unavailable",
            "climate.some_other_hvac": "cool",  # no marker match
            "climate.wiring_centre": "heat",  # not a thermostat
        }
        thermostats, _ = self._app(climates=climates)._discover_entities()
        self.assertEqual(thermostats, [THERMOSTAT, "climate.claudias_room_thermostat"])

    def test_a_renamed_or_added_thermostat_needs_no_config(self):
        thermostats, _ = self._app(
            climates={THERMOSTAT: "heat", LIVING_ROOM: "off"}
        )._discover_entities()
        self.assertEqual(thermostats, [THERMOSTAT, LIVING_ROOM])

    def test_finds_thermostat_battery_but_ignores_other_sensors(self):
        sensors = {
            THERMOSTAT_BATTERY: "25",
            "sensor.bedroom_thermostat_battery": "75",  # marker, but not the _battery_level id
            "sensor.bedroom_thermostat_humidity": "45",  # wrong suffix
            CONTROLLER: ADDRESS,  # no marker, wrong suffix
            "sensor.front_door_battery_level": "80",  # right suffix, no marker
        }
        _, battery = self._app(sensors=sensors)._discover_entities()
        self.assertEqual(battery, [THERMOSTAT_BATTERY])

    def test_exclude_entities_keeps_the_ac_climate_out(self):
        climates = {THERMOSTAT: "heat", AC: "cool"}
        # The A/C carries the thermostat marker, so only the knob keeps it out.
        thermostats, _ = self._app(climates=climates)._discover_entities()
        self.assertEqual(set(thermostats), {THERMOSTAT, AC})

        thermostats, _ = self._app(climates=climates, exclude=[AC])._discover_entities()
        self.assertEqual(thermostats, [THERMOSTAT])

    def test_exclude_entities_drops_a_battery_sensor_too(self):
        ac_battery = "sensor.air_conditioner_thermostat_battery_level"
        sensors = {THERMOSTAT_BATTERY: "25", ac_battery: "50"}
        _, battery = self._app(sensors=sensors, exclude=[ac_battery])._discover_entities()
        self.assertEqual(battery, [THERMOSTAT_BATTERY])

    def test_a_failing_domain_lookup_does_not_block_the_other(self):
        app = self._app(sensors={THERMOSTAT_BATTERY: "25"})
        working = app.get_state

        def get_state(entity=None, **kw):
            if entity == "climate":
                raise RuntimeError("boom")
            return working(entity)

        app.get_state = get_state
        thermostats, battery = app._discover_entities()
        self.assertEqual(thermostats, [])
        self.assertEqual(battery, [THERMOSTAT_BATTERY])
        app.log.assert_called_once()


class ShippedConfig(unittest.TestCase):
    """initialize() fed with the arguments salus_health.yaml actually ships."""

    def _args(self):
        text = (Path(__file__).resolve().parents[1] / "salus_health.yaml").read_text()
        args = dict(yaml.safe_load(text)["SalusHealth"])
        args["state_file"] = _write_state_file(self, {})
        return args

    def test_the_controller_is_the_first_wiring_centre_zone_sensor(self):
        self.assertEqual(self._args()["controller_entity"], CONTROLLER)

    def test_default_exclude_keeps_the_ac_out_of_the_watch_list(self):
        app = _initialized_app(
            self._args(),
            climates={THERMOSTAT: "heat", AC: "unavailable"},
            sensors={CONTROLLER: ADDRESS},
        )
        watched = [call.args[1] for call in app.listen_state.call_args_list]
        self.assertEqual(watched, [THERMOSTAT, CONTROLLER])
        app.run_in.assert_not_called()  # an unavailable A/C would have armed an offline timer

    def test_watches_every_thermostat_battery_and_the_one_controller_and_logs_the_counts(self):
        app = _initialized_app(self._args(), climates=_house_climates(), sensors=_house_sensors())
        watched = {call.args[1] for call in app.listen_state.call_args_list}
        expected = {CONTROLLER}
        for room in ROOMS:
            expected.add(f"climate.{room}_thermostat")
            expected.add(f"sensor.{room}_thermostat_battery_level")
        self.assertEqual(watched, expected)
        app.log.assert_called_once_with(
            "SalusHealth watching 7 thermostat, 1 controller, 7 battery entities", level="INFO"
        )

    def test_an_unavailable_wiring_centre_arms_an_offline_timer(self):
        app = _initialized_app(
            self._args(), climates={THERMOSTAT: "heat"}, sensors={CONTROLLER: "unavailable"}
        )
        armed = [call.kwargs["entity_id"] for call in app.run_in.call_args_list]
        self.assertEqual(armed, [CONTROLLER])

    def test_an_unknown_wiring_centre_arms_nothing(self):
        app = _initialized_app(
            self._args(), climates={THERMOSTAT: "heat"}, sensors={CONTROLLER: "unknown"}
        )
        app.run_in.assert_not_called()
        app.create_task.assert_not_called()


class RoomLabelAndController(unittest.TestCase):
    def test_thermostat_label_strips_suffix_and_thermostat_word(self):
        app = _base_app()
        self.assertEqual(app._room_label(THERMOSTAT), "Bedroom")
        self.assertEqual(app._room_label("climate.claudias_room_thermostat"), "Claudias Room")
        self.assertEqual(app._room_label(LIVING_ROOM), "Living Room")
        self.assertEqual(app._room_label(THERMOSTAT_BATTERY), "Bedroom")

    def test_only_the_configured_entity_is_the_controller(self):
        app = _base_app()
        self.assertTrue(app._is_controller(CONTROLLER))
        self.assertFalse(app._is_controller("sensor.wiring_centre_zone_2"))
        self.assertFalse(app._is_controller(THERMOSTAT))


class LinkState(unittest.TestCase):
    """What a watched entity's state says about the link."""

    def test_unavailable_is_offline(self):
        self.assertEqual(sh.SalusHealth._link("unavailable"), sh.OFFLINE)

    def test_every_other_state_is_online_including_hvac_off_and_zone_addresses(self):
        for state in ("off", "heat", "auto", "cool", "heat_cool", "dry", "fan_only", ADDRESS):
            with self.subTest(state=state):
                self.assertEqual(sh.SalusHealth._link(state), sh.ONLINE)

    def test_unknown_and_none_hold(self):
        for state in ("unknown", None):
            with self.subTest(state=state):
                self.assertIsNone(sh.SalusHealth._link(state))


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

    def test_unavailable_arms_a_timer_for_offline_minutes(self):
        for entity, online in ((THERMOSTAT, "heat"), (CONTROLLER, ADDRESS)):
            with self.subTest(entity=entity):
                app = self._app()
                app._on_connectivity_change(entity, "state", online, "unavailable", {})
                self.assertEqual(len(self.run_in_calls), 1)
                cb, delay, kw = self.run_in_calls[0]
                self.assertEqual(cb, app._maybe_alert_offline)
                self.assertEqual(delay, 600)  # 10 min default, in seconds
                self.assertEqual(kw.get("entity_id"), entity)
                self.assertIn(entity, app._offline_timer_handles)

    def test_hvac_off_is_online_and_never_arms_a_timer(self):
        app = self._app()
        app._on_connectivity_change(THERMOSTAT, "state", "heat", "off", {})
        self.assertEqual(self.run_in_calls, [])

    def test_blip_shorter_than_threshold_does_not_alert(self):
        for entity, online in ((THERMOSTAT, "heat"), (CONTROLLER, ADDRESS)):
            with self.subTest(entity=entity):
                app = self._app()
                app._on_connectivity_change(entity, "state", online, "unavailable", {})
                app._on_connectivity_change(entity, "state", "unavailable", online, {})
                app.cancel_timer.assert_called_once()
                self.assertNotIn(entity, app._offline_timer_handles)

                # Even if the (cancelled) timer's callback somehow still fires late, the
                # current state is online now, so _maybe_alert_offline must refuse to alert.
                app.get_state = lambda e, **kw: online
                app._maybe_alert_offline({"entity_id": entity})
                app.create_task.assert_not_called()

    def test_unknown_neither_arms_nor_disarms(self):
        for entity, online in ((THERMOSTAT, "heat"), (CONTROLLER, ADDRESS)):
            with self.subTest(entity=entity):
                app = self._app()
                app._on_connectivity_change(entity, "state", online, "unknown", {})
                self.assertEqual(self.run_in_calls, [])
                app.cancel_timer.assert_not_called()

                app._offline_timer_handles[entity] = "handle-x"
                app._on_connectivity_change(entity, "state", "unavailable", "unknown", {})
                app.cancel_timer.assert_not_called()
                self.assertIn(entity, app._offline_timer_handles)


class OfflineAlertFiring(unittest.TestCase):
    def _app(self, state="unavailable"):
        app = _base_app()
        app.get_state = lambda e, **kw: state
        return app

    def test_sustained_offline_fires_once(self):
        for entity in (THERMOSTAT, CONTROLLER):
            with self.subTest(entity=entity):
                app = self._app("unavailable")
                app._maybe_alert_offline({"entity_id": entity})
                app.create_task.assert_called_once()
                self.assertTrue(app._offline_alerted[entity])
                app._save_state.assert_called_once()

    def test_does_not_fire_twice_for_the_same_episode(self):
        app = self._app("unavailable")
        app._maybe_alert_offline({"entity_id": THERMOSTAT})
        app._maybe_alert_offline({"entity_id": THERMOSTAT})
        app.create_task.assert_called_once()

    def test_no_alert_if_state_recovered_before_timer_fired(self):
        for entity, online in ((THERMOSTAT, "heat"), (THERMOSTAT, "off"), (CONTROLLER, ADDRESS)):
            with self.subTest(entity=entity, state=online):
                app = self._app(online)
                app._maybe_alert_offline({"entity_id": entity})
                app.create_task.assert_not_called()

    def test_no_alert_if_state_is_unknown_when_the_timer_fires(self):
        for entity in (THERMOSTAT, CONTROLLER):
            with self.subTest(entity=entity):
                app = self._app("unknown")
                app._maybe_alert_offline({"entity_id": entity})
                app.create_task.assert_not_called()
                self.assertFalse(app._offline_alerted.get(entity, False))

    def test_missing_entity_id_in_kwargs_is_a_noop(self):
        app = self._app("unavailable")
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
        app._offline_alerted[THERMOSTAT] = True
        app._on_connectivity_change(THERMOSTAT, "state", "unavailable", "heat", {})
        app.create_task.assert_called_once()
        self.assertFalse(app._offline_alerted[THERMOSTAT])
        app._save_state.assert_called_once()

    def test_hvac_off_counts_as_back_online(self):
        app = self._app()
        app._offline_alerted[THERMOSTAT] = True
        app._on_connectivity_change(THERMOSTAT, "state", "unavailable", "off", {})
        app.create_task.assert_called_once()
        self.assertFalse(app._offline_alerted[THERMOSTAT])

    def test_wiring_centre_recovery_notifies_and_clears_flag(self):
        app = self._app()
        app._offline_alerted[CONTROLLER] = True
        app._on_connectivity_change(CONTROLLER, "state", "unavailable", ADDRESS, {})
        app.create_task.assert_called_once()
        self.assertFalse(app._offline_alerted[CONTROLLER])
        app._save_state.assert_called_once()

    def test_recovery_without_prior_alert_does_not_notify(self):
        for entity, online in ((THERMOSTAT, "heat"), (CONTROLLER, ADDRESS)):
            with self.subTest(entity=entity):
                app = self._app()
                app._on_connectivity_change(entity, "state", "unavailable", online, {})
                app.create_task.assert_not_called()

    def test_unknown_after_an_alert_neither_recovers_nor_clears_the_flag(self):
        for entity in (THERMOSTAT, CONTROLLER):
            with self.subTest(entity=entity):
                app = self._app()
                app._offline_alerted[entity] = True
                app._on_connectivity_change(entity, "state", "unavailable", "unknown", {})
                app.create_task.assert_not_called()
                app._save_state.assert_not_called()
                self.assertTrue(app._offline_alerted[entity])


class OfflineSurvivesRestart(unittest.TestCase):
    """_init_connectivity replays the same decision listen_state's callback would
    have made for whatever transition happened while AppDaemon was down/restarting,
    using the persisted _offline_alerted flag to avoid duplicate or lost alerts."""

    def _app(self, state, alerted_before, entity=THERMOSTAT):
        app = _base_app()
        app.get_state = lambda e, **kw: state
        app.run_in = MagicMock(return_value="handle-1")
        if alerted_before:
            app._offline_alerted[entity] = True
        return app

    def test_still_unavailable_and_already_alerted_does_not_rearm_or_renotify(self):
        for entity in (THERMOSTAT, CONTROLLER):
            with self.subTest(entity=entity):
                app = self._app("unavailable", alerted_before=True, entity=entity)
                app._init_connectivity(entity)
                app.run_in.assert_not_called()
                app.create_task.assert_not_called()
                self.assertTrue(app._offline_alerted[entity])

    def test_still_unavailable_and_not_yet_alerted_arms_a_fresh_timer(self):
        for entity in (THERMOSTAT, CONTROLLER):
            with self.subTest(entity=entity):
                app = self._app("unavailable", alerted_before=False, entity=entity)
                app._init_connectivity(entity)
                app.run_in.assert_called_once()
                app.create_task.assert_not_called()

    def test_recovered_while_restarting_sends_recovery_once(self):
        for entity, state in ((THERMOSTAT, "heat"), (THERMOSTAT, "off"), (CONTROLLER, ADDRESS)):
            with self.subTest(entity=entity, state=state):
                app = self._app(state, alerted_before=True, entity=entity)
                app._init_connectivity(entity)
                app.create_task.assert_called_once()
                self.assertFalse(app._offline_alerted[entity])
                app._save_state.assert_called_once()

    def test_online_and_never_alerted_does_nothing(self):
        for entity, state in ((THERMOSTAT, "heat"), (CONTROLLER, ADDRESS)):
            with self.subTest(entity=entity):
                app = self._app(state, alerted_before=False, entity=entity)
                app._init_connectivity(entity)
                app.create_task.assert_not_called()
                app.run_in.assert_not_called()

    def test_unknown_at_startup_does_nothing(self):
        for entity in (THERMOSTAT, CONTROLLER):
            with self.subTest(entity=entity):
                app = self._app("unknown", alerted_before=False, entity=entity)
                app._init_connectivity(entity)
                app.create_task.assert_not_called()
                app.run_in.assert_not_called()


class OfflineNotifyWording(unittest.IsolatedAsyncioTestCase):
    def _app(self):
        app = _base_app()
        app._notifier = FakeNotifier()
        return app

    async def test_thermostat_offline_wording(self):
        app = self._app()
        await app._notify_offline(THERMOSTAT)
        self.assertEqual(len(app._notifier.calls), 1)
        call = app._notifier.calls[0]
        self.assertEqual(call["title"], "Thermostat offline")
        self.assertEqual(call["message"], "Bedroom thermostat has been offline for 10+ min.")
        self.assertEqual(call["target"], "mikkel")

    async def test_wiring_centre_offline_says_all_heating_depends_on_it(self):
        app = self._app()
        await app._notify_offline(CONTROLLER)
        call = app._notifier.calls[0]
        self.assertEqual(call["title"], "Wiring centre offline")
        self.assertEqual(
            call["message"],
            "The wiring centre has been offline for 10+ min - all room heating depends on it.",
        )
        self.assertEqual(call["target"], "mikkel")

    async def test_recovery_wording_thermostat_vs_wiring_centre(self):
        app = self._app()
        await app._notify_recovery(THERMOSTAT)
        await app._notify_recovery(CONTROLLER)
        titles = [c["title"] for c in app._notifier.calls]
        self.assertEqual(titles, ["Thermostat back online", "Wiring centre back online"])
        self.assertEqual(
            app._notifier.calls[0]["message"], "Bedroom thermostat reconnected after being offline."
        )
        self.assertEqual(
            app._notifier.calls[1]["message"],
            "The wiring centre reconnected - heating control is restored for all rooms.",
        )


class BatteryAlerting(unittest.TestCase):
    """The entity's state is a percent (0/10/25/50/75/100); battery_warn_percent
    (default 40, so 25% alerts and 50% does not) is compared against it, and the
    percent itself is the once-per-drop hysteresis value kept in
    _battery_alerted_percent."""

    def _app(self):
        return _base_app()

    def test_fires_once_at_threshold(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "25")
        app.create_task.assert_called_once()
        self.assertEqual(app._battery_alerted_percent[THERMOSTAT_BATTERY], 25)

    def test_staying_at_the_same_percent_does_not_renag(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "25")
        app._check_battery(THERMOSTAT_BATTERY, "25")
        app.create_task.assert_called_once()

    def test_further_drop_alerts_again(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "25")
        app._check_battery(THERMOSTAT_BATTERY, "10")
        self.assertEqual(app.create_task.call_count, 2)
        self.assertEqual(app._battery_alerted_percent[THERMOSTAT_BATTERY], 10)

    def test_rise_that_stays_low_keeps_the_lowest_alerted_percent(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "10")
        app._check_battery(THERMOSTAT_BATTERY, "25")  # partial charge, still low
        app._check_battery(THERMOSTAT_BATTERY, "10")
        app.create_task.assert_called_once()
        self.assertEqual(app._battery_alerted_percent[THERMOSTAT_BATTERY], 10)

    def test_above_threshold_never_alerts(self):
        for percent in ("50", "75", "100"):
            with self.subTest(percent=percent):
                app = self._app()
                app._check_battery(THERMOSTAT_BATTERY, percent)
                app.create_task.assert_not_called()
                self.assertEqual(app._battery_alerted_percent, {})

    def test_recovery_above_threshold_clears_and_rearms_for_a_future_drop(self):
        app = self._app()
        app._check_battery(THERMOSTAT_BATTERY, "25")  # first low -> alert
        app._check_battery(THERMOSTAT_BATTERY, "100")  # recharged
        self.assertEqual(app._battery_alerted_percent, {})
        app._check_battery(THERMOSTAT_BATTERY, "25")  # drops again later -> alert again
        self.assertEqual(app.create_task.call_count, 2)

    def test_unknown_unavailable_and_none_never_alert(self):
        app = self._app()
        for raw in ("unknown", "unavailable", None):
            app._check_battery(THERMOSTAT_BATTERY, raw)
        app.create_task.assert_not_called()
        self.assertEqual(app._battery_alerted_percent, {})


class BatteryNotifyWording(unittest.IsolatedAsyncioTestCase):
    async def test_message_states_the_percent(self):
        app = _base_app()
        app._notifier = FakeNotifier()
        await app._notify_battery(THERMOSTAT_BATTERY, 25)
        call = app._notifier.calls[0]
        self.assertEqual(call["title"], "Thermostat battery low")
        self.assertEqual(call["message"], "Bedroom thermostat battery is at 25% - charge soon.")
        self.assertEqual(call["target"], "mikkel")


class ParseBatteryLevel(unittest.TestCase):
    def test_numeric_strings_parse(self):
        self.assertEqual(sh.SalusHealth._parse_battery_level("25"), 25)
        self.assertEqual(sh.SalusHealth._parse_battery_level("100"), 100)
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

    def test_save_then_load_round_trips_both_dicts(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._offline_alerted = {THERMOSTAT: True, CONTROLLER: False}
        app._battery_alerted_percent = {THERMOSTAT_BATTERY: 10}
        app._save_state()

        reloaded = self._app(path)._load_state()
        self.assertEqual(reloaded["offline_alerted"], {THERMOSTAT: True, CONTROLLER: False})
        self.assertEqual(reloaded["battery_alerted_percent"], {THERMOSTAT_BATTERY: 10})
        self.assertEqual(set(reloaded), {"offline_alerted", "battery_alerted_percent"})

    def test_save_leaves_no_tmp_file_behind(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._app(path)
        app._offline_alerted = {}
        app._battery_alerted_percent = {}
        app._save_state()
        self.assertFalse(os.path.exists(path + ".tmp"))


class RestoredStateOnInitialize(unittest.TestCase):
    """initialize() reading a persisted state file."""

    def test_battery_percent_survives_a_restart(self):
        path = _write_state_file(self, {"battery_alerted_percent": {THERMOSTAT_BATTERY: 25}})
        app = _initialized_app({"state_file": path}, sensors={THERMOSTAT_BATTERY: "25"})
        app.create_task.assert_not_called()

        app._check_battery(THERMOSTAT_BATTERY, "10")  # a further drop still alerts
        app.create_task.assert_called_once()

    def test_old_0_to_5_battery_state_is_ignored(self):
        path = _write_state_file(self, {"battery_alerted_level": {THERMOSTAT_BATTERY: 2}})
        app = _initialized_app({"state_file": path}, sensors={THERMOSTAT_BATTERY: "25"})

        # Read as a percent, a stored 2 would swallow this alert (25 is not below 2).
        app.create_task.assert_called_once()
        self.assertEqual(app._battery_alerted_percent, {THERMOSTAT_BATTERY: 25})
        with open(path) as f:
            saved = json.load(f)
        self.assertNotIn("battery_alerted_level", saved)
        self.assertEqual(saved["battery_alerted_percent"], {THERMOSTAT_BATTERY: 25})

    def test_old_problem_alerted_key_is_ignored_and_not_written_back(self):
        path = _write_state_file(
            self,
            {
                "offline_alerted": {THERMOSTAT: False},
                "problem_alerted": {"binary_sensor.claudias_room_thermostat_problem": True},
            },
        )
        app = _initialized_app(
            {"state_file": path},
            climates={THERMOSTAT: "heat"},
            sensors={THERMOSTAT_BATTERY: "25"},
        )

        app.create_task.assert_called_once()  # the battery alert; no fault-register push
        self.assertFalse(hasattr(app, "_problem_alerted"))
        with open(path) as f:
            saved = json.load(f)
        self.assertNotIn("problem_alerted", saved)
        self.assertEqual(saved["offline_alerted"], {THERMOSTAT: False})

    def test_offline_flags_for_entities_no_longer_watched_are_dropped(self):
        path = _write_state_file(
            self,
            {
                "offline_alerted": {
                    "binary_sensor.control_centre": True,
                    "binary_sensor.bedroom_thermostat_connectivity": True,
                    THERMOSTAT: True,
                }
            },
        )
        app = _initialized_app({"state_file": path}, climates={THERMOSTAT: "unavailable"})

        self.assertEqual(app._offline_alerted, {THERMOSTAT: True})
        app.run_in.assert_not_called()  # already alerted: no fresh timer
        app.create_task.assert_not_called()

    def test_wiring_centre_alert_flag_survives_a_restart(self):
        path = _write_state_file(self, {"offline_alerted": {CONTROLLER: True}})
        app = _initialized_app({"state_file": path}, sensors={CONTROLLER: "unavailable"})

        self.assertEqual(app._offline_alerted, {CONTROLLER: True})
        app.run_in.assert_not_called()  # already alerted: no fresh timer
        app.create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
