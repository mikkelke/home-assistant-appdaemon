# tests/test_heating_controller.py - HeatingController: live writes, wiring, publishing, the
# decision log, forecast handling, the plan end to end, the nudge buttons, helper and thermostat
# signals, the alerts, the season advice and the state file. Same __new__ + monkeypatched-callables
# harness as test_salus_health.py, with a small fake Home Assistant behind get_state. The planning
# rules, the heat in transit included, are tested in test_heating_plan.py, the signal rules in
# test_heating_signals.py, the alert rules in test_heating_alerts.py and the season rules in
# test_heating_season.py.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import asyncio
import ast
import copy
import dataclasses
import json
import math
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
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

import heating_controller as hc  # noqa: E402
import heating_forecast as hf  # noqa: E402
import heating_plan as hp  # noqa: E402

TZ = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=TZ)
HERE = Path(__file__).resolve().parent
APPS = HERE.parent
FIXTURE = json.loads((HERE / "fixtures" / "open_meteo_forecast.json").read_text())
ZONES = ("family", "kristines_room", "claudias_room", "bathroom", "guest_bathroom")
THERMOSTATS = {
    "family": "climate.family_room_thermostat",
    "kristines_room": "climate.kristines_room_thermostat",
    "claudias_room": "climate.claudias_room_thermostat",
    "bathroom": "climate.bathroom_thermostat",
    "guest_bathroom": "climate.guest_bathroom_thermostat",
}
FEELS = {
    "family": ["sensor.living_room_feel"],
    "kristines_room": ["sensor.kristines_room_feel"],
    "claudias_room": ["sensor.claudias_room_feel"],
    "bathroom": ["sensor.bathroom_feel"],
    "guest_bathroom": ["sensor.guest_bathroom_feel"],
}
HOME_LAT, HOME_LON = 55.6841, 12.5666  # a made-up point in central Copenhagen
BUTTONS = {
    ("kristines_room", "warmer"): "input_button.heating_kristines_room_warmer",
    ("kristines_room", "colder"): "input_button.heating_kristines_room_colder",
    ("claudias_room", "warmer"): "input_button.heating_claudias_room_warmer",
    ("claudias_room", "colder"): "input_button.heating_claudias_room_colder",
}
# Helpers that exist in Home Assistant for this controller.
HELPERS = {
    "input_boolean.heating_season",
    "input_boolean.heating_live",
    "input_number.heating_target_family",
    "input_number.heating_target_kristines_room",
    "input_number.heating_target_claudias_room",
    "input_number.heating_target_bathroom",
    "input_number.heating_target_guest_bathroom",
    "input_datetime.heating_party_start",
    "input_datetime.heating_party_end",
    "input_number.heating_bedroom_ceiling",
    "input_boolean.party",
} | set(BUTTONS.values())
# Home Assistant accounts. Kristine's person entity carries no user_id, so hers comes from the
# yaml; AppDaemon's is the one the yaml ships.
APP_ID = "f4fda494358943beaf8ad2c70db099f8"
MIKKEL_ID = "11111111111111111111111111111111"
CLAUDIA_ID = "33333333333333333333333333333333"
KRISTINE_ID = "5bee78148825439488b70f92565e6c26"
STRANGER_ID = "44444444444444444444444444444444"
AGENT_ID = "f3cc2e6da7c849aba3f366f044725316"
PRESS_TIME = "2026-10-01T10:00:00+00:00"
MIKKEL_PHONE = "device_tracker.of9p"
SUPPLY_PROBE = "sensor.heating_room_floor_heating_supply"
COMMON_RETURN_PROBE = "sensor.heating_room_floor_heating_return"
PUMP_POWER = "sensor.floor_heating_relay_power"


def zones_with_family_night_offset() -> dict:
    zones = copy.deepcopy(
        yaml.safe_load((APPS / "heating_controller.yaml").read_text())["HeatingController"]["zones"]
    )
    zones["family"]["profile"] = {
        "lead_hours": 4,
        "periods": [{"from": "23:00", "to": "10:00", "offset": -0.5}],
    }
    return zones


def zones_with_several_family_rooms() -> dict:
    zones = copy.deepcopy(
        yaml.safe_load((APPS / "heating_controller.yaml").read_text())["HeatingController"]["zones"]
    )
    zones["family"]["control_air"] = [
        "sensor.living_room_feel",
        "sensor.dining_room_feel",
        "sensor.kitchen_feel",
    ]
    zones["family"]["control_air_weights"] = {
        "sensor.living_room_feel": 19,
        "sensor.dining_room_feel": 18.5,
        "sensor.kitchen_feel": 18.5,
    }
    return zones


def zones_without_family_transit() -> dict:
    zones = copy.deepcopy(
        yaml.safe_load((APPS / "heating_controller.yaml").read_text())["HeatingController"]["zones"]
    )
    del zones["family"]["transit"]
    return zones


def shipped_args(tmp: Path) -> dict:
    args = dict(yaml.safe_load((APPS / "heating_controller.yaml").read_text())["HeatingController"])
    args["state_file"] = str(tmp / "state.json")
    args["log_dir"] = str(tmp / "heating_log")
    return args


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def advance(self, **kw):
        self.now += timedelta(**kw)

    def set(self, hour, minute=0, day=17):
        self.now = datetime(2026, 10, day, hour, minute, tzinfo=TZ)


class FakeHA:
    """Entity store with AppDaemon's get_state semantics."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.entities: dict = {}

    def set(self, entity, state, attrs=None, updated=None, changed=None):
        stamp = (updated or self.clock.now).isoformat()
        previous = self.entities.get(entity)
        if changed is not None:
            last_changed = changed.isoformat()
        elif previous and previous["state"] == str(state):
            last_changed = previous["last_changed"]
        else:
            last_changed = stamp
        self.entities[entity] = {
            "entity_id": entity,
            "state": str(state),
            "attributes": dict(attrs or {}),
            "last_updated": stamp,
            "last_changed": last_changed,
        }

    def update_attrs(self, entity, **attrs):
        e = self.entities[entity]
        self.set(entity, e["state"], {**e["attributes"], **attrs})

    def remove(self, entity):
        self.entities.pop(entity, None)

    def get_state(self, entity_id=None, attribute=None, **kw):
        e = self.entities.get(entity_id)
        if e is None:
            return None
        if attribute is None:
            return e["state"]
        if attribute == "all":
            return copy.deepcopy(e)
        return e["attributes"].get(attribute)


def house(ha: FakeHA):
    """A quiet October afternoon: season off, every sensor fresh, air 22.2, thermostats 22.8
    and taking 5 to 24 (25 in the bathroom)."""
    ha.set("input_boolean.heating_season", "off")
    ha.set("input_boolean.heating_live", "off")
    ha.set("input_number.heating_bedroom_ceiling", "21.0")
    for zone, value in zip(ZONES, ("21.5", "20.5", "21.5", "22.0", "20.0")):
        ha.set(f"input_number.heating_target_{zone}", value)
    # a new date-and-time helper without an initial value reads midnight of the day it was made
    ha.set("input_datetime.heating_party_start", "2026-10-17 00:00:00")
    ha.set("input_datetime.heating_party_end", "2026-10-17 00:00:00")
    ha.set("input_boolean.party", "off")
    ha.set("zone.home", "0", {"latitude": HOME_LAT, "longitude": HOME_LON})
    for zone, thermostat in THERMOSTATS.items():
        ha.set(
            thermostat,
            "heat",
            {"current_temperature": 22.8, "temperature": 15.0, "hvac_action": "idle"},
        )
        stem = thermostat.split(".")[1]
        lo, hi = {"bathroom": (5, 25)}.get(zone, (5, 24))
        ha.set(f"number.{stem}_min_heat_setpoint", lo)
        ha.set(f"number.{stem}_max_heat_setpoint", hi)
        for feel in FEELS[zone]:
            ha.set(feel, "22.2", {"stale": "false", "sun_hit": "false", "window_open": "false"})
        ha.set(f"sensor.{zone}_floor_thermometer_temperature", "21.0")
    ha.set("sensor.living_room_floor_thermometer_temperature", "21.0")
    ha.set("sensor.dining_room_floor_thermometer_temperature", "21.0")
    ha.set("climate.living_room_thermostat", "off", {"current_temperature": 22.0})
    ha.set("person.mikkel", "home", {"user_id": MIKKEL_ID})
    ha.set(MIKKEL_PHONE, "home")
    ha.set("person.claudia", "home", {"user_id": CLAUDIA_ID})
    ha.set("person.kristine", "home", {})
    for button in BUTTONS.values():
        ha.set(button, PRESS_TIME)
    for opening in (
        "binary_sensor.living_room_window_contact",
        "binary_sensor.kitchen_window_contact",
        "binary_sensor.dining_room_window_1_contact",
        "binary_sensor.dining_room_window_2_contact",
        "binary_sensor.dining_room_window_3_contact",
        "binary_sensor.rooftop_door_1_contact",
        "binary_sensor.kristines_room_window_contact",
        "binary_sensor.rooftop_door_2_contact",
        "binary_sensor.bathroom_window_contact",
    ):
        ha.set(opening, "off")
    ha.set("sensor.bedroom_feel", "20.8", {"stale": "false", "window_open": "false"})
    ha.set("sensor.kitchen_feel", "22.2", {"stale": "false", "air_band": "good"})
    ha.set("sensor.gw2000a_outdoor_temperature", "11.3")
    ha.set("weather.forecast_home", "cloudy", {"temperature": 11.7})
    ha.set("sensor.floor_heating_relay_power", "1.6")


class Harness:
    """A HeatingController with initialize() run for real against the fake house."""

    def __init__(
        self, testcase, args=None, clock=None, with_house=True, forecast=True, settle=False,
        **overrides
    ):
        self.tmp = Path(tempfile.mkdtemp(prefix="heating-test-"))
        self.testcase = testcase
        testcase.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        testcase.addCleanup(self._close_tasks)
        self.clock = clock or Clock()
        self.ha = FakeHA(self.clock)
        if with_house:
            house(self.ha)
        self.logs: list = []
        self.published: list = []
        self.tasks: list = []
        self.urls: list = []
        self.presses = 0
        self.pushes_seen = 0
        self.notifier = MagicMock()
        self.notifier.notify = AsyncMock()
        self.payload = copy.deepcopy(FIXTURE)
        self.fetch_error = None
        self.calls: list = []
        self.service_result = {"success": True}

        app = hc.HeatingController.__new__(hc.HeatingController)
        app.args = args if args is not None else {**shipped_args(self.tmp), **overrides}
        if not settle:
            app.args = {**app.args, "call": {**app.args["call"], "settle_minutes": 0}}
        app.log = lambda msg, level="INFO", **kw: self.logs.append((level, msg))
        app.get_app = lambda name: self.notifier
        app.get_now = lambda: self.clock.now
        app.get_state = self.ha.get_state
        app.listen_state = MagicMock()
        app.listen_event = MagicMock()
        app.run_every = MagicMock()
        app.run_in = MagicMock()
        app.run_daily = MagicMock()
        app.create_task = lambda coro: self.tasks.append(coro)
        app.set_state = lambda entity, state=None, replace=None, attributes=None, **kw: (
            self.published.append(
                {"entity": entity, "state": state, "replace": replace, "attributes": attributes}
            )
        )
        app.call_service = MagicMock(side_effect=self._call_service)
        for name in (
            "turn_on",
            "turn_off",
            "toggle",
            "select_option",
            "set_value",
            "set_textvalue",
        ):
            setattr(app, name, MagicMock(side_effect=AssertionError("unexpected service helper")))
        app._http_get_json = self._http
        self.app = app
        app.initialize()
        if forecast:
            self.fetch()

    def _close_tasks(self):
        for coro in self.tasks:
            coro.close()
        self.tasks = []

    def _http(self, url):
        self.urls.append(url)
        if self.fetch_error:
            raise self.fetch_error
        return copy.deepcopy(self.payload)

    def _call_service(self, service, **data):
        self.calls.append({"service": service, **data})
        return copy.deepcopy(self.service_result)

    def echo(self, index=-1, **attrs):
        """Apply a recorded climate call and deliver the device's state callback."""
        call = self.calls[index]
        zone = next(name for name, entity in THERMOSTATS.items() if entity == call["entity_id"])
        if "temperature" in call:
            attrs = {"temperature": call["temperature"], **attrs}
        self.report(zone, state=call.get("hvac_mode"), **attrs)

    def restart(self, settle=False):
        self.app.terminate()
        restarted = Harness(
            self.testcase, args=self.app.args, clock=self.clock, with_house=False, forecast=False,
            settle=settle,
        )
        restarted.ha.entities = copy.deepcopy(self.ha.entities)
        restarted.fetch()
        return restarted

    def fetch(self):
        self.app._fetch_forecast({})

    def tick(self, **advance):
        if advance:
            self.clock.advance(**advance)
        self.app._tick({})

    def at(self, hour, minute=0, day=17):
        self.clock.set(hour, minute, day)
        return self

    def last(self, entity):
        for p in reversed(self.published):
            if p["entity"] == entity:
                return p
        raise AssertionError(f"{entity} was never published")

    def zone(self, name):
        return self.last(f"sensor.heating_zone_{name}")

    def attrs(self, name):
        return self.zone(name)["attributes"]

    def log_lines(self, kind=None):
        files = sorted(Path(self.app.args["log_dir"]).glob("decisions-*.jsonl"))
        out = []
        for f in files:
            out += [json.loads(line) for line in f.read_text().splitlines()]
        return [r for r in out if kind is None or r["type"] == kind]

    def run_tasks(self):
        tasks, self.tasks = self.tasks, []
        for coro in tasks:
            asyncio.run(coro)

    def press(self, button, user_id=None, parent_id=None, old=None, new=None):
        """A press of a nudge button, as the raw state_changed event Home Assistant fires."""
        self.presses += 1
        before = old if old is not None else self.ha.entities[button]["state"]
        after = new if new is not None else f"{self.clock.now.isoformat()}#{self.presses}"
        self.ha.set(button, after)
        self.app._on_button(
            "state_changed", self.event(button, before, after, user_id, parent_id), {}
        )

    def helper(self, entity, old, new, user_id=None, parent_id=None, attrs=None):
        """A change of an input_* helper: the entity takes the new value, the event announces it."""
        self.ha.set(entity, new, attrs)
        self.app._on_helper("state_changed", self.event(entity, old, new, user_id, parent_id), {})

    @staticmethod
    def event(entity, old, new, user_id, parent_id):
        return {
            "entity_id": entity,
            "old_state": None if old is None else {"state": str(old)},
            "new_state": {
                "state": str(new),
                "context": {"id": "c", "user_id": user_id, "parent_id": parent_id},
            },
        }

    def service_call(self, zone, service, user_id=None, parent_id=None, entity=None, **data):
        """A climate service call as AppDaemon's call_service event hands it over."""
        entity = entity or THERMOSTATS[zone]
        self.app._on_service_call(
            "call_service",
            {
                "domain": "climate",
                "service": service,
                "service_data": dict(data, entity_id=entity),
                "metadata": {"context": {"id": "c", "user_id": user_id, "parent_id": parent_id}},
            },
            {},
        )

    def report(self, zone, state=None, **attrs):
        """The thermostat reports a new state or attributes, as a state callback delivers it."""
        entity = THERMOSTATS[zone]
        current = self.ha.entities[entity]
        self.ha.set(
            entity, current["state"] if state is None else state, {**current["attributes"], **attrs}
        )
        self.app._on_state(entity, "all", None, copy.deepcopy(self.ha.entities[entity]), {})

    def signals(self, zone=None):
        return [r for r in self.log_lines("signal") if zone is None or r["zone"] == zone]

    def effects(self, zone=None):
        return [r["effect"] for r in self.signals(zone)]

    def season(self, tick=True):
        self.ha.set("input_boolean.heating_season", "on")
        if tick:
            self.tick()
        return self

    def warnings(self):
        return [m for lvl, m in self.logs if lvl in ("WARNING", "ERROR")]


def assert_publishable(testcase, value, path="attributes"):
    """AppDaemon 4.5.13 drops 0, False and None anywhere in an attribute, and empty lists."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert_publishable(testcase, v, f"{path}.{k}")
    elif isinstance(value, list):
        testcase.assertTrue(value, f"{path} is an empty list")
        for i, v in enumerate(value):
            assert_publishable(testcase, v, f"{path}[{i}]")
    else:
        testcase.assertIsNotNone(value, path)
        testcase.assertNotIsInstance(value, bool, path)
        if isinstance(value, (int, float)):
            testcase.assertNotEqual(value, 0, path)


class ShippedYaml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.args = shipped_args(Path("/tmp"))
        cls.cfg = hp.parse_config(cls.args)

    def test_the_owner_decisions_are_what_ships(self):
        z = self.cfg.zones
        self.assertEqual([z[n].base_c for n in ZONES], [21.5, 20.5, 21.5, 22.0, 20.0])
        self.assertEqual([z[n].lower_c for n in ZONES], [19.0, 19.0, 19.0, 19.0, 18.0])
        self.assertEqual([z[n].upper_c for n in ZONES], [24.0, 24.0, 24.0, 25.0, 24.0])
        self.assertIsNone(z["family"].protected_min_c)
        self.assertEqual(z["family"].protected_min_offset_c, -0.5)
        self.assertEqual(self.cfg.sun_term_factor, 0.5)
        self.assertEqual(self.cfg.keypad_relock_minutes, 15)
        for n in ("kristines_room", "claudias_room"):
            self.assertEqual((z[n].nudge.minimum, z[n].nudge.maximum), (-1.0, 1.0))
        self.assertEqual(self.cfg.bedroom.ceiling_default_c, 21.0)
        self.assertEqual(self.cfg.bedroom.morning_offset_c, 0.5)
        self.assertEqual(self.cfg.bedroom.release_below_c, 20.5)

    def test_the_signal_and_alert_settings_ship_with_the_decided_numbers(self):
        cfg = self.cfg
        self.assertEqual((cfg.owner, cfg.notify_target), ("mikkel", "mikkel"))
        self.assertEqual(cfg.appdaemon_user_id, APP_ID)
        self.assertEqual(cfg.user_ids, {KRISTINE_ID: "kristine", AGENT_ID: "agent"})
        self.assertEqual(
            (cfg.supply_temp_entity, cfg.return_temp_entity),
            ("sensor.heating_room_floor_heating_supply", "sensor.heating_room_floor_heating_return"),
        )
        self.assertEqual(cfg.living_monitor_entity, "climate.living_room_thermostat")
        self.assertEqual(cfg.alerts, hp.AlertConfig())
        floors = {n: zc.evenness_floors for n, zc in cfg.zones.items() if zc.evenness_floors}
        self.assertEqual(floors, {"family": (LIVING_FLOOR, DINING_FLOOR)})
        residents = {n: zc.residents for n, zc in cfg.zones.items() if zc.residents}
        self.assertEqual(
            residents, {"kristines_room": ("kristine",), "claudias_room": ("claudia",)}
        )

    def test_the_season_advisor_ships_with_the_decided_numbers(self):
        a = self.cfg.season_advisor
        self.assertEqual(a.zones, ("family", "kristines_room", "claudias_room"))
        self.assertEqual(a.margins, {"kristines_room": 0.2})
        self.assertEqual((a.margin("family"), a.margin("claudias_room")), (0.5, 0.5))
        self.assertEqual((a.evaluate_at, a.mornings, a.start_below_c), (8 * 60, 2, 12.0))
        self.assertEqual((a.stop_from, a.stop_until), ((3, 1), (9, 30)))
        self.assertEqual((a.look_back_days, a.idle_days, a.idle_hours), (7, 5, 2.0))
        self.assertEqual((a.stop_min_c, a.season_min_days, a.reask_days), (5.0, 7, 3))
        self.assertEqual(a, hp.parse_config(self.args).season_advisor)

    def test_the_family_room_is_one_zone_that_drives_both_floor_loops(self):
        self.assertEqual(tuple(self.cfg.zones), ZONES)
        family = self.cfg.zones["family"]
        self.assertEqual(family.thermostat, THERMOSTATS["family"])
        self.assertEqual(family.control_air, tuple(FEELS["family"]))
        self.assertEqual(family.floor_sensors, (LIVING_FLOOR, DINING_FLOOR))
        self.assertEqual(family.evenness_floors, (LIVING_FLOOR, DINING_FLOOR))
        self.assertEqual(family.floor_max_c, 24.5)
        self.assertEqual(family.target_entity, "input_number.heating_target_family")
        self.assertEqual(
            family.openings,
            (
                "binary_sensor.living_room_window_contact",
                "binary_sensor.kitchen_window_contact",
                "binary_sensor.dining_room_window_1_contact",
                "binary_sensor.dining_room_window_2_contact",
                "binary_sensor.dining_room_window_3_contact",
                "binary_sensor.rooftop_door_1_contact",
            ),
        )
        self.assertEqual(self.cfg.party.zone, "family")

    def test_the_family_air_is_the_living_room_alone_and_no_zone_weighs_rooms(self):
        self.assertEqual(self.cfg.zones["family"].control_air, ("sensor.living_room_feel",))
        self.assertEqual({zc.control_air_weights for zc in self.cfg.zones.values()}, {()})

    def test_only_the_family_zone_has_several_floors_and_every_zone_reads_one_room(self):
        self.assertEqual(
            [n for n, zc in self.cfg.zones.items() if len(zc.floor_sensors) > 1], ["family"]
        )
        self.assertEqual(
            [n for n, zc in self.cfg.zones.items() if len(zc.control_air) > 1], []
        )

    def test_the_advisor_judges_every_zone_on_the_air_the_controller_plans_by(self):
        a = self.cfg.season_advisor
        for zone in a.zones:
            self.assertEqual(a.rooms[zone], self.cfg.zones[zone].control_air, zone)

    def test_family_holds_one_aim_all_day(self):
        self.assertFalse(self.cfg.zones["family"].profile.periods)

    def test_bathroom_is_warm_from_night_to_morning_20_by_day_and_21_5_in_the_evening(self):
        zc = self.cfg.zones["bathroom"]
        self.assertEqual(zc.profile.lead_hours, 0)
        self.assertFalse(zc.profile.outside_is_lower_limit)
        self.assertEqual(
            [(p.start, p.end, p.offset) for p in zc.profile.periods],
            [(150, 9 * 60, 0.0), (9 * 60, 18 * 60, -2.0), (18 * 60, 150, -0.5)],
        )

    def test_bathroom_holds_21_while_mikkel_is_out_and_17_while_he_travels(self):
        zc = self.cfg.zones["bathroom"]
        self.assertEqual((zc.base_c, zc.profile.default_offset, zc.lower_c), (22.0, -1.0, 19.0))
        self.assertEqual(zc.profile.travel, hp.Travel(MIKKEL_PHONE, 50.0, 17.0))

    def test_only_the_bathroom_waits_for_its_user_to_be_home(self):
        waiting = {n: zc.profile.when_home for n, zc in self.cfg.zones.items()}
        self.assertEqual(
            {n: entities for n, entities in waiting.items() if entities},
            {"bathroom": ("person.mikkel", MIKKEL_PHONE)},
        )

    def test_no_zone_is_held_back_for_the_bedroom(self):
        guarded = [n for n, zc in self.cfg.zones.items() if zc.guard.enabled]
        self.assertEqual(guarded, [])

    def test_sun_cuts_only_where_the_glass_looks_at_the_sun(self):
        enabled = [n for n, zc in self.cfg.zones.items() if zc.sun_cut.enabled]
        self.assertEqual(enabled, ["family"])

    def test_the_family_protected_minimum_is_half_a_degree_under_the_helper(self):
        family = self.cfg.zones["family"]
        self.assertEqual(hp.protected_min(family, 22.0, 0.0, family.lower_c), 21.5)
        self.assertEqual(hp.protected_min(family, 21.5, 0.0, family.lower_c), 21.0)

    def shipped_day(self, t_mean, wind, ghi, sun_hours):
        """The family zone's budget of one day at the shipped trust in the sun, and the cut
        windows that day is worth."""
        family = self.cfg.zones["family"]
        weather = hf.DayWeather(NOW.date(), 24, t_mean, max(0.0, hf.HDD_BASE_C - t_mean), wind, ghi)
        starts = [NOW.replace(hour=h).timestamp() for h in sun_hours]
        budget = hp.day_budget(family, weather, starts, self.cfg.sun_term_factor)
        cut = hp.evaluate_cut(family, [budget], NOW.timestamp(), None)
        return budget, cut.windows

    def test_the_shipped_sun_cut_takes_a_cold_windy_day_with_five_bright_hours(self):
        b, windows = self.shipped_day(4.9, 17.9, 1.31, range(10, 15))
        self.assertEqual(b.n_sun, 5)
        self.assertAlmostEqual(b.h_dull, 9.27, delta=0.05)
        self.assertAlmostEqual(b.u_eq, 0.39, delta=0.01)
        self.assertAlmostEqual(b.s_sun, 2.13, delta=0.01)
        self.assertAlmostEqual(b.s_credit, 1.06, delta=0.01)
        self.assertAlmostEqual(b.skipped_h, 0.77, delta=0.01)
        self.assertAlmostEqual(b.ratio, 1.38, delta=0.01)
        self.assertEqual(len(windows), 1)

    def test_the_shipped_sun_cut_skips_a_cold_windy_day_with_three_dim_hours(self):
        b, windows = self.shipped_day(5.5, 21.2, 0.76, range(11, 14))
        self.assertEqual(b.n_sun, 3)
        self.assertAlmostEqual(b.h_dull, 9.27, delta=0.05)
        self.assertAlmostEqual(b.u_eq, 0.39, delta=0.01)
        self.assertAlmostEqual(b.s_sun, 1.23, delta=0.01)
        self.assertAlmostEqual(b.s_credit, 0.61, delta=0.01)
        self.assertAlmostEqual(b.ratio, 0.80, delta=0.01)
        self.assertLess(b.ghi_kwh, self.cfg.zones["family"].sun_cut.min_ghi_kwh)
        self.assertEqual(windows, ())

    def test_the_shipped_sun_cut_leaves_a_dull_day_alone(self):
        b, windows = self.shipped_day(4.9, 17.9, 0.3, [])
        self.assertEqual(b.n_sun, 0)
        self.assertIsNone(b.window_start)
        self.assertEqual(windows, ())

    def test_the_call_rule_and_the_airing_lockout_ship_with_the_decided_numbers(self):
        cfg = self.cfg
        self.assertEqual(cfg.call, hp.CallConfig(0.2, 20.0, 20.0, 30.0, 6.0, 2.0))
        self.assertEqual(cfg.writes_per_hour, 4)
        self.assertEqual(cfg.write_stable_minutes, 10)
        self.assertEqual(cfg.window, hp.WindowConfig(2.0, 60.0, 0.3, 10.0, 1.0, 4.0))

    def test_every_zone_ships_the_loops_its_call_is_stopped_on(self):
        def probe(loop):
            return f"sensor.heating_room_{loop}_return"

        shipped = {
            n: (
                [(loop.name, loop.entity, loop.lpm) for loop in zc.transit.loops],
                zc.transit.tau_minutes,
                zc.transit.k_per_kwh,
            )
            for n, zc in self.cfg.zones.items()
        }
        family = [("zone_1", probe("zone_1"), 2.0), ("zone_4", probe("zone_4"), 2.0)]
        self.assertEqual(
            shipped,
            {
                "family": (family, 40.0, 0.2),
                "kristines_room": ([("zone_2", probe("zone_2"), 1.2)], 25.0, 1.2),
                "claudias_room": ([("zone_3", probe("zone_3"), 0.95)], 25.0, 1.4),
                "bathroom": ([("zone_7", probe("zone_7"), 1.2)], 20.0, 2.7),
                "guest_bathroom": ([("zone_6", probe("zone_6"), 0.35)], 20.0, 3.2),
            },
        )
        self.assertEqual(self.cfg.supply_temp_entity, SUPPLY_PROBE)
        self.assertEqual(self.cfg.relay_power_entity, PUMP_POWER)

    def test_no_zone_ships_the_settings_of_the_model_that_was_replaced(self):
        for name, raw in self.args["zones"].items():
            for key in ("trim", "weather_offset", "warm_hold"):
                self.assertNotIn(key, raw, f"{name}: {key}")
        self.assertNotIn("long_open_minutes", self.args["window"])

    def test_helper_entities_are_ones_that_exist(self):
        wanted = {self.cfg.season_entity, self.cfg.bedroom.ceiling_entity}
        wanted.update(zc.target_entity for zc in self.cfg.zones.values())
        wanted.update(
            [self.cfg.party.start_entity, self.cfg.party.end_entity, self.cfg.party.switch_entity]
        )
        buttons = {e for zc in self.cfg.zones.values() for e in zc.buttons.values()}
        self.assertEqual(buttons, set(BUTTONS.values()))
        self.assertLessEqual(wanted | buttons, HELPERS)

    def test_no_coordinates_or_secrets_in_the_yaml(self):
        text = (APPS / "heating_controller.yaml").read_text()
        self.assertIsNone(re.search(r"\b\d{2}\.\d{3,}\b", text))
        self.assertIsNone(re.search(r"[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}", text))
        self.assertNotRegex(text, r"(?i)latitude|longitude|password|secret|api[_-]?key")


class ServiceBoundary(unittest.TestCase):
    def test_only_the_controller_calls_the_two_permitted_services(self):
        services = set()
        for name in ("controller", "plan", "signals", "alerts", "season"):
            tree = ast.parse((APPS / f"heating_{name}.py").read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                self.assertNotIn(
                    node.func.attr,
                    ("turn_on", "turn_off", "toggle", "select_option", "set_value", "set_textvalue"),
                    name,
                )
                if node.func.attr == "call_service":
                    self.assertEqual(name, "controller")
                    self.assertIsInstance(node.args[0], ast.Constant)
                    services.add(node.args[0].value)
        self.assertEqual(services, {"climate/set_temperature", "climate/set_hvac_mode"})

    def test_the_rule_modules_have_no_io(self):
        for name in ("plan", "signals", "alerts", "season"):
            tree = ast.parse((APPS / f"heating_{name}.py").read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    modules = []
                for module in modules:
                    self.assertNotIn(
                        module.split(".")[0], ("appdaemon", "requests", "socket", "subprocess"), name
                    )
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ("open", "print", "input"), name)

    def test_a_season_with_windows_a_party_and_a_hold_back_never_calls_one(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.ha.set("binary_sensor.rooftop_door_1_contact", "on")
        h.ha.set("input_boolean.party", "on")
        h.app._loop.hold = True
        h.tick()
        h.tick(minutes=3)
        h.tick(minutes=5)
        for name in ("call_service", "turn_on", "turn_off", "toggle", "select_option", "set_value"):
            getattr(h.app, name).assert_not_called()


class Initialize(unittest.TestCase):
    def test_wires_listeners_timers_and_the_restart_hook(self):
        h = Harness(self, forecast=False)
        watched = {c.args[1] for c in h.app.listen_state.call_args_list}
        self.assertEqual(watched, h.app._watched)
        for c in h.app.listen_state.call_args_list:
            self.assertEqual(c.kwargs.get("attribute"), "all")
            self.assertNotIn("old", c.kwargs)
        self.assertIn("climate.family_room_thermostat", watched)
        self.assertIn("binary_sensor.rooftop_door_2_contact", watched)
        self.assertIn("sensor.bedroom_feel", watched)
        self.assertIn("input_boolean.party", watched)
        every = [c.args for c in h.app.run_every.call_args_list]
        self.assertIn((h.app._tick, "now+30", 300), every)
        self.assertIn((h.app._fetch_forecast, "now+5", 3600), every)
        h.app.run_daily.assert_called_once_with(h.app._daily_rollup, "03:00:00")
        events = [(c.args[0], c.args[1], c.kwargs) for c in h.app.listen_event.call_args_list]
        self.assertIn((h.app._on_plugin_started, "plugin_started", {}), events)
        self.assertIn((h.app._on_plugin_stopped, "plugin_stopped", {}), events)
        self.assertIn((h.app._on_service_call, "call_service", {"domain": "climate"}), events)
        for button in BUTTONS.values():
            self.assertIn((h.app._on_button, "state_changed", {"entity_id": button}), events)
        owned = {e["entity_id"] for cb, name, e in events if cb == h.app._on_helper}
        self.assertEqual(
            owned,
            {
                "input_boolean.heating_season",
                "input_boolean.heating_live",
                "input_number.heating_bedroom_ceiling",
                "input_boolean.party",
                "input_datetime.heating_party_start",
                "input_datetime.heating_party_end",
            }
            | {f"input_number.heating_target_{z}" for z in ZONES},
        )
        self.assertEqual(len(events), 2 + 1 + len(BUTTONS) + len(owned))
        self.assertTrue(any("live-enabled" in m and "zones" in m for _lvl, m in h.logs))

    def test_where_the_bathroom_user_is_counts_as_an_input(self):
        h = Harness(self, forecast=False)
        watched = {c.args[1] for c in h.app.listen_state.call_args_list}
        for entity in ("person.mikkel", MIKKEL_PHONE):
            self.assertIn(entity, watched)
            self.assertIn(entity, h.app._triggers)

    def test_a_bad_config_logs_an_error_and_starts_nothing(self):
        args = shipped_args(Path("/tmp"))
        args["zones"]["family"]["lower_limit_c"] = 30.0
        h = Harness(self, args=args, forecast=False)
        h.app.listen_state.assert_not_called()
        self.assertTrue(any(lvl == "ERROR" and "config error" in m for lvl, m in h.logs))

    def test_log_strings_are_ascii(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        for _lvl, msg in h.logs:
            msg.encode("ascii")


class LiveWrites(unittest.TestCase):
    def ready(self, mode="heat", **overrides):
        h = Harness(self, **{"live_zones": ["family"], **overrides})
        entity = THERMOSTATS["family"]
        h.ha.set(entity, mode, {**h.ha.entities[entity]["attributes"], "preset_mode": "schedule"})
        h.ha.set(h.app.cfg.live_switch_entity, "on")
        h.season()
        return h

    def written(self, mode="heat", **overrides):
        h = self.ready(mode, **overrides)
        h.tick(minutes=10)
        self.assertEqual(len(h.calls), 1)
        return h

    def confirmed(self, mode="heat", **overrides):
        h = self.written(mode, **overrides)
        h.clock.advance(seconds=5)
        h.echo()
        h.tick()
        self.assertEqual(h.attrs("family")["last_write"]["result"], "confirmed")
        return h

    @staticmethod
    def cold(h, air="20.5", zone="family"):
        for feel in FEELS[zone]:
            h.ha.set(feel, air, {"stale": "false", "sun_hit": "false", "window_open": "false"})

    @staticmethod
    def scarce(h, tokens=1.0, zone="family"):
        h.app._zones[zone].bucket.tokens = tokens

    def timeout(self, h):
        calls = [c for c in h.app.run_in.call_args_list if len(c.args) > 1 and c.args[1] == 180]
        self.assertTrue(calls, "the write needs a 180 second read-back deadline")
        timer = calls[-1]
        h.clock.advance(seconds=180)
        timer.args[0](timer.kwargs)

    def test_every_zone_ships_live_and_the_switch_is_watched(self):
        h = Harness(self)
        self.assertEqual(h.app.cfg.live_zones, ("family", "bathroom", "kristines_room", "claudias_room", "guest_bathroom"))
        watched = {c.args[1] for c in h.app.listen_state.call_args_list}
        self.assertIn("input_boolean.heating_live", watched)
        self.assertIn("input_boolean.heating_live", h.app._triggers)

    def test_empty_live_zones_never_write_even_with_the_switch_on(self):
        h = self.ready(live_zones=[])
        h.tick(minutes=10)
        self.assertEqual(h.calls, [])
        self.assertEqual(h.last("sensor.heating_controller")["state"], "shadow")
        self.assertEqual(h.last("sensor.heating_controller")["attributes"]["zones_live"], ["<none>"])

    def test_off_unknown_unavailable_and_missing_switch_disable_writes(self):
        for state in ("off", "unknown", "unavailable", None):
            with self.subTest(state=state):
                h = Harness(self, live_zones=["family"])
                if state is None:
                    h.ha.remove("input_boolean.heating_live")
                else:
                    h.ha.set("input_boolean.heating_live", state)
                h.season()
                h.tick(minutes=10)
                self.assertEqual(h.calls, [])
                self.assertEqual(h.attrs("family")["live"], "false")

    def test_a_custom_live_switch_controls_the_writer(self):
        h = self.written(live_switch_entity="input_boolean.heating_write")
        self.assertIn("input_boolean.heating_write", h.app._watched)
        self.assertEqual(h.calls[0]["entity_id"], THERMOSTATS["family"])

    def test_going_live_turns_only_family_on_at_the_minimum_while_the_room_is_warm(self):
        h = self.written(mode="off")
        self.assertEqual(
            h.calls,
            [{"service": "climate/set_temperature", "entity_id": THERMOSTATS["family"],
              "temperature": 5.0, "hvac_mode": "heat"}],
        )
        self.assertEqual(h.attrs("family")["live"], "true")
        self.assertTrue(all(h.attrs(z)["live"] == "false" for z in ZONES if z != "family"))
        controller = h.last("sensor.heating_controller")
        self.assertEqual(controller["state"], "live")
        self.assertEqual(controller["attributes"]["zones_live"], ["family"])
        self.assertEqual(int(controller["attributes"]["writes_today"]), 1)
        self.assertIn("would_writes_today", controller["attributes"])
        self.assertEqual(h.attrs("family")["thermostat_mode"], "off")
        self.assertEqual(float(h.attrs("family")["thermostat_setpoint"]), 15.0)
        self.assertEqual(h.attrs("family")["held_off"], "false")
        self.assertEqual(h.attrs("family")["last_write"]["result"], "pending")
        self.assertTrue(all("live" in r for r in h.log_lines("decision")))

    def test_a_thermostat_already_in_heat_receives_no_mode_field(self):
        h = self.written()
        self.assertEqual(
            h.calls[0],
            {"service": "climate/set_temperature", "entity_id": THERMOSTATS["family"],
             "temperature": 5.0},
        )

    def test_own_write_is_recorded_before_the_service_call(self):
        h = self.ready(mode="off")
        original = h.app.call_service.side_effect

        def inspect(service, **data):
            rt = h.app._zones["family"]
            self.assertIsNotNone(rt.own_write)
            self.assertEqual((rt.own_write.value, rt.own_write.mode), (data["temperature"], "heat"))
            self.assertIs(rt.pending_write, rt.own_write)
            return original(service, **data)

        h.app.call_service.side_effect = inspect
        h.tick(minutes=10)
        self.assertEqual(len(h.calls), 1)

    def test_echo_confirms_and_salus_preset_echo_is_not_a_restore(self):
        h = self.confirmed(mode="off")
        record = h.attrs("family")["last_write"]
        self.assertEqual(record["setpoint"], 5.0)
        self.assertEqual(record["mode"], "heat")
        self.assertEqual(float(record["latency_s"]), 5.0)
        self.assertIn("at", record)
        self.assertIsNone(h.app._zones["family"].pending_write)
        h.clock.advance(seconds=3)
        h.report("family", preset_mode="permanent_hold")
        self.assertNotIn("restore", h.effects("family"))
        self.assertTrue(all(r["source"] == "own_write" for r in h.signals("family")))
        self.assertNotIn("last_signal", h.attrs("family"))
        h.tick(minutes=10)
        self.assertEqual(len(h.calls), 1)
        for published in h.published:
            assert_publishable(self, published["attributes"])

    def test_reported_state_controls_the_gate_even_when_last_planned_disagrees(self):
        h = self.ready()
        h.ha.update_attrs(THERMOSTATS["family"], temperature=5.0)
        h.app._zones["family"].last_planned = 19.0
        h.app._zones["family"].decision_key = None
        h.tick(minutes=10)
        self.assertEqual(h.calls, [])
        self.assertEqual(
            [r for r in h.log_lines("decision") if r["zone"] == "family"][-1]["gate"],
            "unchanged",
        )

    def test_a_pending_write_prevents_a_second_send(self):
        h = self.written()
        for _ in range(2):
            h.tick(seconds=60)
        self.assertEqual(len(h.calls), 1)
        self.assertIsNotNone(h.app._zones["family"].pending_write)
        h.echo()
        h.tick()
        self.assertIsNone(h.app._zones["family"].pending_write)

    def test_an_echo_after_the_deadline_cannot_confirm_even_if_the_timer_is_delayed(self):
        h = self.written()
        h.clock.advance(seconds=181)
        h.echo()
        self.assertEqual(h.app._zones["family"].last_write["result"], "timeout")
        self.assertIsNone(h.app._zones["family"].pending_write)
        day = h.app._zones["family"].days["2026-10-17"]
        self.assertEqual((day.confirmed, day.failed), (0, 1))

    def test_an_open_window_writes_the_minimum_within_one_evaluation_of_being_debounced(self):
        h = self.ready()
        self.cold(h)
        h.tick()
        self.assertEqual(h.calls[-1]["temperature"], 23.5)
        h.echo()
        h.ha.set("binary_sensor.living_room_window_contact", "on")
        h.tick()
        self.assertEqual(len(h.calls), 1)
        h.tick(minutes=2)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[-1]["service"], "climate/set_temperature")
        self.assertEqual(h.calls[-1]["temperature"], 5.0)
        self.assertNotIn("hvac_mode", h.calls[-1])
        self.assertEqual(h.attrs("family")["mode"], "window")
        self.assertEqual(h.attrs("family")["call"], "false")

    def test_an_open_window_never_gets_a_high_write_however_cold_the_room_gets(self):
        h = self.ready()
        self.cold(h)
        h.tick()
        h.echo()
        h.ha.set("binary_sensor.living_room_window_contact", "on")
        h.tick()
        h.tick(minutes=2)
        h.echo()
        self.cold(h, "17.0")
        for _ in range(24):
            h.tick(minutes=5)
        self.assertEqual([c["temperature"] for c in h.calls], [23.5, 5.0])
        self.assertEqual(h.attrs("family")["mode"], "window")

    def test_a_window_that_opens_in_a_quiet_zone_writes_nothing(self):
        h = self.confirmed()
        h.ha.set("binary_sensor.living_room_window_contact", "on")
        h.tick()
        h.tick(minutes=2)
        h.tick(minutes=10)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.attrs("family")["mode"], "window")

    def test_foreign_setpoint_changes_are_restored_at_once(self):
        for caller in (None, APP_ID, MIKKEL_ID):
            with self.subTest(caller=caller):
                h = self.confirmed()
                h.clock.advance(seconds=1)
                if caller is not None:
                    h.service_call("family", "set_temperature", caller, temperature=20.0)
                h.report("family", temperature=20.0)
                changed_at = h.clock.now.timestamp()
                self.assertEqual(h.effects("family")[-1], "restore")
                self.assertEqual(h.app._zones["family"].desired_since, changed_at)
                h.tick(seconds=1)
                self.assertEqual(len(h.calls), 2)
                self.assertEqual(h.calls[-1]["temperature"], 5.0)
                self.assertEqual(h.zone("family")["state"], 21.5)
                self.assertEqual(h.app._zones["family"].nudge, 0.0)
                self.assertEqual(h.ha.get_state("input_number.heating_target_family"), "21.5")

    def test_foreign_mode_changes_restore_heat_at_once(self):
        for mode in ("off", "auto"):
            with self.subTest(mode=mode):
                h = self.confirmed()
                h.clock.advance(seconds=1)
                h.report("family", state=mode)
                h.tick(seconds=1)
                self.assertEqual(len(h.calls), 2)
                self.assertEqual(h.calls[-1]["hvac_mode"], "heat")
                self.assertEqual(h.attrs("family")["held_off"], "false")

    def test_the_owners_off_latch_survives_restart_until_the_mode_changes(self):
        h = self.confirmed()
        h.service_call("family", "set_hvac_mode", MIKKEL_ID, hvac_mode="off")
        h.report("family", state="off")
        h.tick()
        self.assertEqual(h.attrs("family")["held_off"], "true")
        restarted = h.restart()
        restarted.tick()
        restarted.tick(minutes=10)
        self.assertEqual(restarted.calls, [])
        self.assertEqual(restarted.attrs("family")["held_off"], "true")
        restarted.report("family", state="heat", temperature=20.0)
        restarted.tick()
        self.assertEqual(restarted.attrs("family")["held_off"], "false")
        restarted.tick(minutes=10)
        self.assertEqual(restarted.calls[-1]["temperature"], 5.0)

    def test_a_new_season_releases_the_owners_off_latch(self):
        h = self.confirmed()
        h.service_call("family", "set_hvac_mode", MIKKEL_ID, hvac_mode="off")
        h.report("family", state="off")
        h.ha.set("input_boolean.heating_season", "off")
        h.tick()
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        self.assertEqual(h.attrs("family")["held_off"], "false")
        h.tick(minutes=10)
        self.assertEqual(h.calls[-1].get("hvac_mode"), "heat")

    def test_no_echo_times_out_at_180_seconds_and_the_second_failure_alerts_once(self):
        h = self.written()
        self.scarce(h)
        self.timeout(h)
        self.assertEqual(h.app._zones["family"].last_write["result"], "timeout")
        h.tick()
        self.assertEqual(len(h.calls), 2)
        self.timeout(h)
        h.tick()
        self.assertEqual(len(h.calls), 2)
        h.run_tasks()
        self.assertEqual(h.notifier.notify.await_count, 1)
        self.assertEqual(h.notifier.notify.await_args.kwargs["target"], "mikkel")
        self.assertIn("write:family", h.last("sensor.heating_controller")["attributes"]["alerts_active"])
        events = [r for r in h.log_lines("alert") if r["key"] == "write:family"]
        self.assertEqual([r["event"] for r in events], ["start"])
        h.tick(minutes=24)
        self.assertEqual(len(h.calls), 3)
        h.echo()
        h.tick()
        self.assertNotIn("write:family", h.last("sensor.heating_controller")["attributes"]["alerts_active"])
        events = [r for r in h.log_lines("alert") if r["key"] == "write:family"]
        self.assertEqual([r["event"] for r in events], ["start", "clear"])
        self.assertGreaterEqual(len(h.log_lines("write")), 6)
        self.assertGreaterEqual(len(h.warnings()), 2)

    def test_service_results_without_success_true_are_immediate_failures(self):
        for result in (None, {"success": False}, {}, {"success": 1}):
            with self.subTest(result=result):
                h = self.ready()
                h.service_result = result
                h.tick(minutes=10)
                self.assertEqual(len(h.calls), 1)
                self.assertIsNone(h.app._zones["family"].pending_write)
                self.assertEqual(h.attrs("family")["last_write"]["result"], "failed")
                self.assertTrue(any(level == "WARNING" for level, _ in h.logs))

    def test_a_failed_service_result_overrides_a_synchronous_echo(self):
        h = self.ready(mode="off")
        original = h.app.call_service.side_effect

        def echo_before_returning(service, **data):
            original(service, **data)
            h.echo()
            return {"success": False}

        h.app.call_service.side_effect = echo_before_returning
        h.tick(minutes=10)
        self.assertEqual(h.attrs("family")["last_write"]["result"], "failed")
        day = h.app._zones["family"].days["2026-10-17"]
        self.assertEqual((day.writes, day.confirmed, day.failed), (1, 0, 1))

    def test_device_reversion_is_a_failed_write_and_converges_on_the_next_token(self):
        h = self.confirmed()
        h.clock.advance(seconds=5)
        h.report("family", temperature=15.0)
        h.tick()
        self.assertEqual(h.app._zones["family"].days["2026-10-17"].failed, 1)
        self.assertEqual(h.calls[-1]["temperature"], 5.0)
        self.assertEqual(len(h.calls), 2)

    def test_two_writes_that_echo_then_revert_raise_the_write_alert(self):
        h = self.confirmed()
        self.scarce(h)
        h.clock.advance(seconds=5)
        h.report("family", temperature=15.0)
        h.tick()
        self.assertEqual(len(h.calls), 2)
        h.clock.advance(seconds=5)
        h.echo()
        h.clock.advance(seconds=5)
        h.report("family", temperature=15.0)
        h.tick()
        h.run_tasks()
        self.assertEqual(h.notifier.notify.await_count, 1)
        self.assertIn("write:family", h.last("sensor.heating_controller")["attributes"]["alerts_active"])
        day = h.app._zones["family"].days["2026-10-17"]
        self.assertEqual((day.writes, day.confirmed, day.failed), (2, 0, 2))

    def test_a_confirmed_mode_that_reverts_is_failed_and_retried(self):
        h = self.confirmed(mode="off")
        h.clock.advance(seconds=5)
        h.report("family", state="off")
        self.assertEqual(h.app._zones["family"].last_write["result"], "reverted")
        h.tick()
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[-1]["hvac_mode"], "heat")
        self.assertEqual(h.app._zones["family"].days["2026-10-17"].failed, 1)

    def test_the_owners_off_after_a_mode_write_is_held_and_not_a_failure(self):
        h = self.confirmed(mode="off")
        h.service_call("family", "set_hvac_mode", MIKKEL_ID, hvac_mode="off")
        h.report("family", state="off")
        h.tick(minutes=10)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.attrs("family")["held_off"], "true")
        self.assertEqual(h.app._zones["family"].days["2026-10-17"].failed, 0)

    def test_an_owner_change_to_the_prewrite_value_is_a_restore_not_a_device_reversion(self):
        h = self.confirmed()
        h.service_call("family", "set_temperature", MIKKEL_ID, temperature=15.0)
        h.report("family", temperature=15.0)
        self.assertEqual(h.signals("family")[-1]["source"], "ha")
        self.assertEqual(h.effects("family")[-1], "restore")
        h.tick(seconds=1)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.app._zones["family"].days["2026-10-17"].failed, 0)

    def switch_off(self, h):
        h.ha.set("input_boolean.heating_live", "off")
        entity = "input_boolean.heating_live"
        h.app._on_state(entity, "all", None, copy.deepcopy(h.ha.entities[entity]), {})

    def test_the_kill_switch_stops_a_pending_retry_and_hands_the_aim_back_once(self):
        h = self.written()
        self.switch_off(h)
        self.timeout(h)
        h.app._evaluate_pending({})
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(
            h.calls[-1],
            {"service": "climate/set_temperature", "entity_id": THERMOSTATS["family"],
             "temperature": 21.5},
        )
        self.timeout(h)
        h.tick(minutes=10)
        h.tick(minutes=60)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.attrs("family")["live"], "false")
        self.assertIsNone(h.app._zones["family"].pending_write)

    def test_the_kill_switch_writes_the_aim_of_every_live_zone_once_as_an_ordinary_setpoint(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_live", "on")
        h.season()
        h.tick(minutes=10)
        for zone in ZONES:
            h.echo(next(i for i, c in enumerate(h.calls) if c["entity_id"] == THERMOSTATS[zone]))
        before = len(h.calls)
        self.assertEqual(before, len(ZONES))
        self.switch_off(h)
        h.tick()
        released = {c["entity_id"]: c for c in h.calls[before:]}
        self.assertEqual(len(h.calls) - before, len(ZONES))
        aims = {z: h.attrs(z)["setpoint_now"] for z in ZONES}
        for zone in ZONES:
            call = released[THERMOSTATS[zone]]
            self.assertEqual(call["service"], "climate/set_temperature")
            self.assertEqual(call["temperature"], hp.round_step(aims[zone]), zone)
            self.assertNotIn("hvac_mode", call)
        h.tick(minutes=10)
        h.tick(minutes=30)
        self.assertEqual(len(h.calls) - before, len(ZONES))

    def test_the_kill_switch_does_not_switch_on_what_the_owner_or_the_season_switched_off(self):
        h = self.confirmed()
        h.service_call("family", "set_hvac_mode", MIKKEL_ID, hvac_mode="off")
        h.report("family", state="off")
        h.tick()
        self.assertEqual(h.attrs("family")["held_off"], "true")
        before = len(h.calls)
        self.switch_off(h)
        h.tick()
        self.assertEqual(len(h.calls), before)

    def test_the_kill_switch_hands_a_calling_zone_back_at_the_aim_and_not_at_its_high_setpoint(self):
        h = self.ready()
        self.cold(h)
        h.tick()
        self.assertEqual(h.calls[-1]["temperature"], 23.5)
        h.echo()
        self.switch_off(h)
        h.tick(minutes=1)
        self.assertEqual(h.calls[-1]["temperature"], 21.5)
        self.assertEqual(len(h.calls), 2)

    def test_switching_the_kill_switch_on_again_resumes_the_drive(self):
        h = self.confirmed()
        self.switch_off(h)
        h.tick()
        h.echo()
        h.ha.set("input_boolean.heating_live", "on")
        h.tick()
        h.tick(minutes=10)
        self.assertEqual(h.calls[-1]["temperature"], 5.0)
        self.assertEqual(h.attrs("family")["live"], "true")

    def test_a_restart_with_the_switch_already_off_writes_no_release(self):
        h = self.confirmed()
        self.switch_off(h)
        h.tick()
        h.echo()
        before = len(h.calls)
        restarted = h.restart()
        restarted.tick()
        restarted.tick(minutes=30)
        self.assertEqual(restarted.calls, [])
        self.assertEqual(len(h.calls), before)

    def test_a_restart_while_the_switch_goes_off_still_hands_the_aim_back(self):
        h = self.confirmed()
        h.ha.set("input_boolean.heating_live", "off")
        restarted = h.restart()
        restarted.tick()
        self.assertEqual(len(restarted.calls), 1)
        self.assertEqual(restarted.calls[-1]["temperature"], 21.5)

    def test_offline_drops_pending_and_recovery_reseeds_without_a_signal(self):
        h = self.written()
        h.report("family", state="unavailable")
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "device_offline")
        self.assertIsNone(h.app._zones["family"].pending_write)
        h.tick(minutes=5)
        self.assertEqual(len(h.calls), 1)
        h.report("family", state="heat", temperature=20.0)
        self.assertEqual(h.signals("family"), [])
        h.tick()
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[-1]["temperature"], 5.0)
        h.echo()
        h.tick(minutes=10)
        self.assertEqual(len(h.calls), 2)

    def test_a_home_assistant_restart_blocks_writes_and_reseeds_the_first_report(self):
        h = self.written()
        h.app._on_plugin_stopped("plugin_stopped", {}, {})
        self.assertEqual(h.attrs("family")["mode"], "device_offline")
        self.assertIsNone(h.app._zones["family"].pending_write)
        h.tick(minutes=5)
        self.assertEqual(len(h.calls), 1)
        h.ha.update_attrs(THERMOSTATS["family"], temperature=20.0)
        h.app._on_plugin_started("plugin_started", {}, {})
        h.report("family", temperature=20.0)
        self.assertEqual(h.signals("family"), [])
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(h.calls[-1]["temperature"], 5.0)
        h.echo()
        h.tick(minutes=10)
        self.assertEqual(len(h.calls), 2)

    def test_season_off_is_sent_once_and_remains_sent_across_restart(self):
        h = self.confirmed()
        h.ha.set("input_boolean.heating_season", "off")
        h.tick()
        self.assertEqual(h.calls[-1], {
            "service": "climate/set_hvac_mode", "entity_id": THERMOSTATS["family"],
            "hvac_mode": "off",
        })
        self.assertEqual(len(h.calls), 2)
        h.tick(minutes=1)
        self.assertEqual(len(h.calls), 2)
        restarted = h.restart()
        restarted.tick()
        restarted.tick(minutes=10)
        self.assertEqual(restarted.calls, [])
        restarted.report("family", state="off")
        restarted.tick(minutes=10)
        self.assertEqual(restarted.calls, [])

    def test_a_failed_season_off_is_sent_again(self):
        h = self.confirmed()
        h.service_result = {"success": False}
        h.ha.set("input_boolean.heating_season", "off")
        h.tick()
        self.assertEqual(h.attrs("family")["last_write"]["result"], "failed")
        h.service_result = {"success": True}
        h.tick(minutes=60)
        offs = [c for c in h.calls if c.get("hvac_mode") == "off"]
        self.assertEqual(len(offs), 2)

    def test_invalid_entity_limits_keep_the_zone_planned(self):
        h = self.ready()
        h.ha.set("number.family_room_thermostat_max_heat_setpoint", "3")
        since = h.clock.now.timestamp()
        h.tick(minutes=31)
        h.tick(minutes=31)
        self.assertEqual(h.calls, [])
        later = [
            r for r in h.log_lines("decision")
            if r["zone"] == "family" and datetime.fromisoformat(r["ts"]).timestamp() > since
        ]
        self.assertTrue(later)

    def test_a_mode_only_season_off_echo_can_change_setpoint_and_preset(self):
        h = self.confirmed()
        h.ha.set("input_boolean.heating_season", "off")
        h.tick()
        h.clock.advance(seconds=5)
        h.echo(temperature=5.0)
        h.clock.advance(seconds=3)
        h.report("family", preset_mode="permanent_hold")
        h.tick()
        self.assertEqual(h.attrs("family")["last_write"]["result"], "confirmed")
        self.assertNotIn("restore", h.effects("family"))
        self.assertTrue(all(r["source"] == "own_write" for r in h.signals("family")))
        self.assertEqual(h.attrs("family")["held_off"], "false")

    def test_a_season_off_waiting_for_a_token_survives_restart(self):
        h = self.confirmed()
        while h.app._zones["family"].bucket.take(h.clock.now.timestamp()):
            pass
        h.ha.set("input_boolean.heating_season", "off")
        h.tick()
        self.assertEqual(len(h.calls), 1)
        restarted = h.restart()
        restarted.tick()
        self.assertEqual(restarted.calls, [])
        restarted.tick(minutes=14)
        self.assertEqual(restarted.calls, [])
        restarted.tick(minutes=1)
        self.assertEqual(restarted.calls, [{
            "service": "climate/set_hvac_mode", "entity_id": THERMOSTATS["family"],
            "hvac_mode": "off",
        }])
        restarted.tick(minutes=5)
        self.assertEqual(len(restarted.calls), 1)

    def test_starting_with_the_season_off_never_sends_an_off(self):
        h = Harness(self, live_zones=["family"])
        h.ha.set("input_boolean.heating_live", "on")
        h.tick()
        h.tick(minutes=10)
        self.assertEqual(h.calls, [])

    def test_restart_drops_pending_and_waits_ten_minutes_for_a_nonpriority_write(self):
        h = self.written()
        saved = json.loads(Path(h.app.args["state_file"]).read_text())["zones"]["family"]
        self.assertNotIn("desired_since", saved)
        self.assertNotIn("pending_write", saved)
        restarted = h.restart()
        self.assertIsNone(restarted.app._zones["family"].pending_write)
        restarted.tick()
        restarted.tick(minutes=9, seconds=59)
        self.assertEqual(restarted.calls, [])
        restarted.tick(seconds=1)
        self.assertEqual(len(restarted.calls), 1)
        self.assertEqual(restarted.calls[-1]["temperature"], 5.0)

    def test_terminate_never_writes(self):
        h = self.ready(mode="off")
        h.clock.advance(minutes=10)
        h.app.terminate()
        self.assertEqual(h.calls, [])

    def test_live_writes_obey_both_the_device_and_entity_limits(self):
        for entity_bounds, device_bounds, quiet, calling in (
            ((20.0, 21.0), (5.0, 24.0), 20.0, 21.0),
            ((23.0, 24.0), (5.0, 24.0), 23.0, 23.5),
            ((5.0, 24.0), (5.0, 21.0), 5.0, 21.0),
            ((5.0, 24.0), (23.0, 24.0), 23.0, 24.0),
        ):
            for air, expected in (("24.0", quiet), ("20.5", calling)):
                with self.subTest(entity=entity_bounds, device=device_bounds, air=air):
                    h = self.ready()
                    h.ha.update_attrs(
                        THERMOSTATS["family"], min_temp=entity_bounds[0], max_temp=entity_bounds[1]
                    )
                    h.ha.set("number.family_room_thermostat_min_heat_setpoint", device_bounds[0])
                    h.ha.set("number.family_room_thermostat_max_heat_setpoint", device_bounds[1])
                    self.cold(h, air)
                    h.tick()
                    h.tick(minutes=10)
                    self.assertEqual(h.calls[0]["temperature"], expected)

    def test_nonfinite_or_off_step_drives_are_not_written_and_warn_once(self):
        for value in (float("nan"), float("inf"), float("-inf"), 22.25):
            with self.subTest(value=value):
                h = self.ready()

                def invalid(state, aim, fallback, lower, upper):
                    return value

                with patch.object(hp, "drive_setpoint", side_effect=invalid):
                    h.tick()
                    h.tick(minutes=10)
                    h.tick(minutes=5)
                self.assertEqual(h.calls, [])
                self.assertEqual(len([msg for level, msg in h.logs if level == "WARNING"]), 1)
                self.assertEqual([msg for level, msg in h.logs if level == "ERROR"], [])

    def test_a_nonfinite_target_helper_is_not_written_and_warns_once(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                h = self.ready()
                h.ha.set("input_number.heating_target_family", value)
                h.tick()
                h.tick(minutes=10)
                h.tick(minutes=5)
                self.assertEqual(h.calls, [])
                self.assertEqual(len([msg for level, msg in h.logs if level == "WARNING"]), 1)
                self.assertEqual([msg for level, msg in h.logs if level == "ERROR"], [])

    def test_invalid_entity_limits_never_reach_the_service(self):
        for bounds in (
            {"min_temp": float("nan")}, {"max_temp": float("inf")},
            {"min_temp": 24.0, "max_temp": 20.0}, {"max_temp": 21.25},
        ):
            with self.subTest(bounds=bounds):
                h = self.ready()
                h.ha.update_attrs(THERMOSTATS["family"], **bounds)
                self.cold(h)
                h.tick()
                h.tick(minutes=10)
                self.assertEqual(h.calls, [])
                self.assertEqual(len([msg for level, msg in h.logs if level == "WARNING"]), 1)

    def test_nonfinite_and_inverted_device_limits_never_reach_the_service(self):
        for lower, upper in (("nan", "24"), ("19", "inf"), ("24", "20")):
            with self.subTest(lower=lower, upper=upper):
                h = self.ready()
                h.ha.set("number.family_room_thermostat_min_heat_setpoint", lower)
                h.ha.set("number.family_room_thermostat_max_heat_setpoint", upper)
                h.tick()
                h.tick(minutes=10)
                self.assertEqual(h.calls, [])
                self.assertTrue(any(level == "WARNING" for level, _ in h.logs))
                self.assertEqual([msg for level, msg in h.logs if level == "ERROR"], [])

    def test_daily_counts_cover_writes_confirmation_failure_and_restore(self):
        h = self.confirmed()
        h.report("family", temperature=20.0)
        h.tick(minutes=10)
        self.timeout(h)
        h.at(3, day=18)
        h.app._daily_rollup({})
        rows = {r["zone"]: r for r in h.log_lines("daily")}
        for zone in ("family", "_house"):
            self.assertEqual(
                {key: rows[zone][key] for key in ("writes", "confirmed", "failed", "restores")},
                {"writes": 2, "confirmed": 1, "failed": 1, "restores": 1},
            )


class LiveZones:
    """A harness with some zones live and the room air under the test's control."""

    def live(self, *zones):
        h = Harness(self, live_zones=list(zones))
        h.ha.set(h.app.cfg.live_switch_entity, "on")
        h.season()
        return h

    @staticmethod
    def air(h, value, zone="family"):
        for feel in FEELS[zone]:
            h.ha.set(feel, str(value), {"stale": "false", "sun_hit": "false", "window_open": "false"})

    @staticmethod
    def sent(h, zone=None):
        return [
            c["temperature"]
            for c in h.calls
            if zone is None or c["entity_id"] == THERMOSTATS[zone]
        ]

    @staticmethod
    def decision(h, zone="family"):
        return [r for r in h.log_lines("decision") if r["zone"] == zone][-1]

    @staticmethod
    def iso(hour, minute=0, day=17):
        return datetime(2026, 10, day, hour, minute, tzinfo=TZ).isoformat()


class CallsSurviveRestarts(LiveZones, unittest.TestCase):
    """Home Assistant restarts several times a day. The room sensors come back one by one, and
    a room's feel built from the first one back can be more than a degree off for up to a
    minute, so a call is kept as it was until the house has settled."""

    def calling(self):
        h = Harness(self, live_zones=["family"], settle=True)
        h.ha.set(h.app.cfg.live_switch_entity, "on")
        h.season()
        self.air(h, 20.5)
        h.tick(minutes=4)
        self.assertEqual(h.attrs("family")["call"], "false")
        self.assertEqual(h.app._zones["family"].call_reason, "settling")
        h.tick(minutes=1)
        self.assertEqual(h.attrs("family")["call"], "true")
        h.echo()
        h.tick(minutes=35)
        self.air(h, 21.6)
        h.tick()
        return h

    def test_a_too_warm_first_feel_after_a_restart_does_not_stop_a_call(self):
        h = self.calling()
        writes = len(h.calls)
        self.air(h, 22.7)
        h = h.restart(settle=True)
        h.tick()
        self.assertEqual(h.attrs("family")["call"], "true")
        self.assertEqual(self.decision(h)["gate"], "gated:settling")
        self.air(h, 21.6)
        h.tick(minutes=4)
        h.tick(minutes=2)
        self.assertEqual(h.attrs("family")["call"], "true")
        self.assertEqual(h.app._zones["family"].call_reason, "calling")
        self.assertEqual(len(h.calls), 0)
        self.assertGreater(writes, 0)

    def test_after_home_assistant_comes_back_a_call_is_kept_until_the_house_settles(self):
        h = self.calling()
        writes = len(h.calls)
        h.app._on_plugin_started("plugin_started", {}, {})
        self.air(h, 22.7)
        h.tick(minutes=4)
        self.assertEqual(h.attrs("family")["call"], "true")
        self.assertEqual(len(h.calls), writes)
        h.tick(minutes=1)
        self.assertEqual(h.attrs("family")["call"], "false")
        self.assertEqual(self.sent(h)[-1], 5.0)


class CallEndToEnd(LiveZones, unittest.TestCase):
    def test_a_warm_house_never_calls_and_the_thermostat_stays_at_its_minimum(self):
        h = self.live("family")
        h.tick(minutes=10)
        h.echo()
        for _ in range(12 * 24):
            h.tick(minutes=5)
            self.assertEqual(h.attrs("family")["call"], "false")
        self.assertEqual(self.sent(h), [5.0])

    def test_a_cold_room_starts_a_call_at_once_in_the_first_minutes_of_a_slot(self):
        h = self.live("family")
        self.air(h, 20.5)
        h.tick()
        a = h.attrs("family")
        self.assertEqual((a["call"], a["call_since"]), ("true", self.iso(12)))
        self.assertEqual(self.sent(h), [23.5])
        d = self.decision(h)
        self.assertEqual(
            (d["aim"], d["allowed"], d["call"], d["call_reason"], d["drive"]),
            (21.5, True, True, "start", 23.5),
        )
        self.assertEqual((d["gate"], d["would_write"]), ("priority:call", True))
        self.assertTrue(
            any("family: call starts (start), air 20.5, aim 21.5" in m for _l, m in h.logs)
        )
        for published in h.published:
            assert_publishable(self, published["attributes"])

    def test_zones_that_need_heat_outside_a_slot_window_start_together_at_the_next_slot(self):
        h = self.live("family", "claudias_room")
        h.tick(minutes=10)
        for index in range(len(h.calls)):
            h.echo(index)
        self.air(h, 20.5)
        self.air(h, 20.5, "claudias_room")
        for minutes in (5, 5, 5, 4):
            h.tick(minutes=minutes)
            for zone in ("family", "claudias_room"):
                self.assertEqual(h.attrs(zone)["call"], "false", zone)
                self.assertEqual(h.app._zones[zone].call_reason, "wait_slot", zone)
        h.tick(minutes=1)
        self.assertEqual(h.clock.now, datetime(2026, 10, 17, 12, 30, tzinfo=TZ))
        for zone in ("family", "claudias_room"):
            self.assertEqual(h.attrs(zone)["call_since"], self.iso(12, 30), zone)
        self.assertEqual(self.sent(h, "family")[-1], 23.5)
        self.assertEqual(self.sent(h, "claudias_room")[-1], 23.5)

    def test_a_call_runs_for_at_least_twenty_minutes_and_stops_at_once_after_that(self):
        h = self.live("family")
        self.air(h, 20.5)
        h.tick()
        h.echo()
        self.air(h, 22.0)
        h.tick(minutes=19)
        self.assertEqual((h.attrs("family")["call"], h.app._zones["family"].call_reason), ("true", "min_on"))
        self.assertEqual(self.sent(h), [23.5])
        h.tick(minutes=1)
        a = h.attrs("family")
        self.assertEqual((a["call"], a["call_since"]), ("false", self.iso(12, 20)))
        self.assertEqual(self.sent(h), [23.5, 5.0])
        d = self.decision(h)
        self.assertEqual((d["gate"], d["call_reason"], d["drive"]), ("priority:call", "stop", 5.0))
        self.assertTrue(any("family: call stops (stop), air 22.0" in m for _l, m in h.logs))

    def test_the_air_inside_the_hysteresis_band_neither_starts_nor_stops_a_call(self):
        h = self.live("family")
        self.air(h, 21.4)
        h.tick()
        self.assertEqual(h.attrs("family")["call"], "false")
        self.air(h, 21.2)
        h.tick(minutes=5)
        self.assertEqual(h.attrs("family")["call"], "true")
        h.echo()
        self.air(h, 21.6)
        h.tick(minutes=25)
        self.assertEqual(h.attrs("family")["call"], "true")
        self.air(h, 21.8)
        h.tick(minutes=1)
        self.assertEqual(h.attrs("family")["call"], "false")
        self.assertEqual(self.sent(h), [23.5, 5.0])

    def test_a_stopped_call_is_not_restarted_for_twenty_minutes_and_then_waits_for_a_slot(self):
        h = self.live("family")
        self.air(h, 20.5)
        h.tick()
        h.echo()
        self.air(h, 22.0)
        h.tick(minutes=20)
        h.echo()
        self.assertEqual(h.attrs("family")["call"], "false")
        self.air(h, 20.5)
        reasons = {}
        for _ in range(7):
            h.tick(minutes=5)
            reasons[h.clock.now.strftime("%H:%M")] = (
                h.attrs("family")["call"], h.app._zones["family"].call_reason
            )
        self.assertEqual(reasons["12:30"], ("false", "min_off"))
        self.assertEqual(reasons["12:35"], ("false", "min_off"))
        self.assertEqual(reasons["12:40"], ("false", "wait_slot"))
        self.assertEqual(reasons["12:55"], ("false", "wait_slot"))
        h.tick(minutes=5)
        self.assertEqual(h.clock.now, datetime(2026, 10, 17, 13, 0, tzinfo=TZ))
        a = h.attrs("family")
        self.assertEqual((a["call"], a["call_since"]), ("true", self.iso(13)))

    def test_a_floor_at_its_limit_stops_a_running_call_at_once(self):
        h = self.live("family")
        self.air(h, 20.5)
        h.tick()
        h.echo()
        h.ha.set(LIVING_FLOOR, "23.0")
        h.tick()
        self.assertEqual(h.attrs("family")["call"], "true")
        h.ha.set(LIVING_FLOOR, "24.6")
        h.tick(minutes=1)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["call"]), ("floor_cap", "false"))
        self.assertEqual(self.sent(h), [23.5, 5.0])
        self.assertEqual(self.decision(h)["gate"], "priority:call")

    def test_a_cold_house_does_not_call_while_the_owner_has_the_zone_off(self):
        h = self.live("family")
        self.air(h, 20.5)
        h.tick()
        h.echo()
        h.tick(minutes=5)
        h.service_call("family", "set_hvac_mode", MIKKEL_ID, hvac_mode="off")
        h.report("family", state="off", temperature=15.0)
        h.tick()
        a = h.attrs("family")
        self.assertEqual((a["held_off"], a["call"], a["call_since"]), ("true", "false", self.iso(12, 5)))
        for _ in range(24):
            h.tick(minutes=30)
        self.assertEqual(self.sent(h), [23.5])
        self.assertEqual(self.decision(h)["call_reason"], "not_allowed")
        h.report("family", state="heat", temperature=15.0)
        h.tick()
        self.assertEqual(h.attrs("family")["held_off"], "false")

    def test_the_owner_turning_a_calling_zone_off_is_not_a_restore(self):
        h = self.live("family")
        self.air(h, 20.5)
        h.tick()
        h.echo()
        h.tick(minutes=5)
        h.service_call("family", "set_hvac_mode", MIKKEL_ID, hvac_mode="off")
        h.report("family", state="off", temperature=15.0)
        self.assertEqual(h.effects("family")[-2:], ["none", "off_by_owner"])
        self.assertEqual(h.signals("family")[-2]["who"], "mikkel")
        self.assertEqual(h.app._zones["family"].days["2026-10-17"].restores, 0)

    def test_without_room_air_the_aim_is_written_as_an_ordinary_setpoint(self):
        h = self.live("family")
        old = h.clock.now - timedelta(minutes=200)
        for feel in FEELS["family"]:
            h.ha.set(feel, "unavailable")
        h.ha.set(
            THERMOSTATS["family"],
            "heat",
            {"current_temperature": None, "temperature": 15.0},
            updated=old,
        )
        for floor in (LIVING_FLOOR, DINING_FLOOR):
            h.ha.set(floor, "21.0", updated=old)
        h.tick()
        h.tick(minutes=10)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["call"], a["setpoint_now"]), ("fallback", "false", 21.5))
        self.assertEqual(self.sent(h), [21.5])
        self.assertEqual(self.decision(h)["drive"], 21.5)
        h.echo()
        self.air(h, 22.2)
        h.ha.set(THERMOSTATS["family"], "heat", {"current_temperature": 22.8, "temperature": 21.5})
        h.tick()
        h.tick(minutes=10)
        self.assertEqual(self.sent(h), [21.5, 5.0])

    def test_a_restart_keeps_the_running_call_and_its_clock(self):
        h = self.live("family")
        self.air(h, 20.5)
        h.tick()
        h.echo()
        h.tick(minutes=7)
        restarted = h.restart()
        restarted.tick()
        a = restarted.attrs("family")
        self.assertEqual((a["call"], a["call_since"]), ("true", self.iso(12)))
        self.assertEqual(restarted.calls, [])
        self.air(restarted, 22.5)
        restarted.tick(minutes=12)
        self.assertEqual(restarted.attrs("family")["call"], "true")
        self.assertEqual(restarted.calls, [])
        restarted.tick(minutes=1)
        self.assertEqual(restarted.attrs("family")["call"], "false")
        self.assertEqual(self.sent(restarted), [5.0])

    def test_a_thermostat_that_never_confirms_gets_at_most_four_writes_an_hour(self):
        h = self.live("family")
        self.air(h, 20.5)
        rt = h.app._zones["family"]
        rt.bucket.tokens, rt.bucket.ts = 0.0, h.clock.now.timestamp()
        times = []
        for _ in range(120):
            h.tick(minutes=1)
            times += [h.clock.now] * (len(h.calls) - len(times))
        self.assertGreaterEqual(len(times), 7)
        self.assertEqual(set(self.sent(h)), {23.5})
        for first in times:
            in_the_hour = [t for t in times if first <= t < first + timedelta(hours=1)]
            self.assertLessEqual(len(in_the_hour), 4, first)


def loop_probe(loop):
    return f"sensor.heating_room_{loop}_return"


class HeatInTransitEndToEnd(LiveZones, unittest.TestCase):
    """The family zone calls from 12:00. With 36.0 on the supply and 33.0 on both family returns,
    each of its two loops gives off 2.0 L/min x 69.8 x 3.0 K, and the pump draws 25 W."""

    LOOP_W = 2.0 * 69.8 * 3.0
    FAMILY_W = 2 * LOOP_W
    STEP_KWH = FAMILY_W * 300 / 3.6e6
    DECAY = math.exp(-300 / (40 * 60))

    @staticmethod
    def water(h, supply=36.0, pump=25.0, common=31.4, **returns):
        """The pipe probes, the pump and the outdoor sensor as they read now."""
        h.ha.set(SUPPLY_PROBE, str(supply))
        h.ha.set(COMMON_RETURN_PROBE, str(common))
        h.ha.set(PUMP_POWER, str(pump))
        h.ha.set("sensor.gw2000a_outdoor_temperature", "11.3")
        for loop, value in {"zone_1": 33.0, "zone_4": 33.0, **returns}.items():
            h.ha.set(loop_probe(loop), str(value))

    def calling(self, air=21.56, harness=None):
        """The family zone, called at 12:00 from cold air that then reads `air`."""
        h = harness or self.live("family")
        self.air(h, 20.5)
        h.tick()
        h.echo()
        self.air(h, air)
        return h

    def kwh_after(self, steps):
        q_kwh = 0.0
        for _ in range(steps):
            q_kwh = q_kwh * self.DECAY + self.STEP_KWH
        return q_kwh

    def run_ticks(self, h, count, **water):
        for _ in range(count):
            self.water(h, **water)
            h.tick(minutes=5)

    def stopped(self):
        """The family zone at 12:20, the tick its call stops on the coast."""
        h = self.calling()
        self.run_ticks(h, 4)
        return h

    def test_the_heat_in_transit_builds_up_while_water_flows(self):
        h = self.calling(21.5)
        rt = h.app._zones["family"]
        self.assertEqual(rt.transit.q_kwh, 0.0)
        for steps in (1, 2, 3):
            self.run_ticks(h, 1)
            self.assertAlmostEqual(rt.transit.q_kwh, self.kwh_after(steps))
        a = h.attrs("family")
        self.assertEqual(a["transit_power_w"], f"{self.FAMILY_W:.0f}")
        self.assertEqual(a["transit_q_kwh"], f"{self.kwh_after(3):.3f}")
        self.assertEqual(a["coast_c"], f"{0.2 * self.kwh_after(3):.2f}")

    def test_a_call_stops_on_the_coast_where_the_plain_rule_would_run_on(self):
        h = self.calling()
        for _ in range(3):
            self.run_ticks(h, 1)
            self.assertEqual(h.attrs("family")["call"], "true")
        self.run_ticks(h, 1)
        a = h.attrs("family")
        self.assertEqual(h.clock.now, NOW + timedelta(minutes=20))
        self.assertEqual((a["call"], a["call_since"]), ("false", self.iso(12, 20)))
        self.assertEqual(h.app._zones["family"].call_reason, "stop")
        self.assertEqual(self.sent(h), [23.5, 5.0])

    def test_the_same_air_keeps_a_call_running_when_no_water_flows(self):
        h = self.calling()
        for _ in range(8):
            self.run_ticks(h, 1, pump=1.6)
            self.assertEqual(h.attrs("family")["call"], "true")
        self.assertEqual(h.app._zones["family"].transit.q_kwh, 0.0)
        self.assertEqual(h.attrs("family")["coast_c"], "0.00")
        self.air(h, 21.75)
        self.run_ticks(h, 1, pump=1.6)
        self.assertEqual(h.attrs("family")["call"], "false")

    def test_a_call_that_is_not_yet_at_the_aim_runs_on_with_a_coast(self):
        h = self.calling(21.2)
        for _ in range(8):
            self.run_ticks(h, 1)
            self.assertEqual(h.attrs("family")["call"], "true")

    def test_a_stop_is_logged_with_the_readings_behind_the_coast(self):
        h = self.stopped()
        (line,) = h.log_lines("call_stop")
        q_kwh = self.kwh_after(4)
        self.assertEqual(
            set(line),
            {
                "type", "ts", "zone", "reason", "call_start", "minutes_on", "aim", "air", "floor",
                "outdoor", "pump_w", "supply", "common_return", "loops", "power_w", "q_kwh",
                "coast_c", "k_per_kwh", "tau_minutes", "flowing_zones",
            },
        )
        self.assertEqual(line["ts"], "2026-10-17T12:20:00+02:00")
        self.assertEqual(
            (line["type"], line["zone"], line["reason"]), ("call_stop", "family", "stop")
        )
        self.assertEqual((line["call_start"], line["minutes_on"]), (self.iso(12), 20.0))
        self.assertEqual(
            (line["aim"], line["air"], line["floor"], line["outdoor"]), (21.5, 21.56, 21.0, 11.3)
        )
        self.assertEqual(
            (line["pump_w"], line["supply"], line["common_return"]), (25.0, 36.0, 31.4)
        )
        self.assertEqual(
            line["loops"],
            [
                {"loop": "zone_1", "return": 33.0, "delta": 3.0, "lpm": 2.0, "w": 418.8},
                {"loop": "zone_4", "return": 33.0, "delta": 3.0, "lpm": 2.0, "w": 418.8},
            ],
        )
        self.assertEqual(line["power_w"], 837.6)
        self.assertAlmostEqual(line["q_kwh"], q_kwh, places=4)
        self.assertAlmostEqual(line["coast_c"], 0.2 * q_kwh, places=3)
        self.assertEqual((line["k_per_kwh"], line["tau_minutes"]), (0.2, 40.0))
        self.assertEqual(line["flowing_zones"], ["family"])

    def test_a_call_that_goes_on_logs_no_stop(self):
        h = self.calling(21.2)
        self.run_ticks(h, 6)
        self.assertEqual(h.log_lines("call_stop"), [])

    def test_a_stop_names_every_zone_that_had_water_flowing(self):
        h = self.live("family", "claudias_room")
        self.air(h, 20.5)
        self.air(h, 20.5, "claudias_room")
        h.tick()
        for index in range(len(h.calls)):
            h.echo(index)
        self.air(h, 21.56)
        self.run_ticks(h, 4, zone_3=32.0)
        (line,) = h.log_lines("call_stop")
        self.assertEqual(line["zone"], "family")
        self.assertEqual(line["flowing_zones"], ["family", "claudias_room"])
        self.assertEqual(h.attrs("claudias_room")["call"], "true")

    def test_fifteen_minutes_into_a_call_the_lowest_returns_are_logged_once(self):
        h = self.calling(21.4)
        q_kwh = 0.0
        for first, fourth in [(33.5, 34.0), (31.0, 33.5), (32.0, 33.0)]:
            self.assertEqual(h.log_lines("call_start"), [])
            self.run_ticks(h, 1, zone_1=first, zone_4=fourth)
            watts = 2.0 * 69.8 * (36.0 - first) + 2.0 * 69.8 * (36.0 - fourth)
            q_kwh = q_kwh * self.DECAY + watts * 300 / 3.6e6
        (line,) = h.log_lines("call_start")
        self.assertEqual(
            (line["type"], line["ts"], line["zone"], line["call_start"]),
            ("call_start", "2026-10-17T12:15:00+02:00", "family", self.iso(12)),
        )
        self.assertEqual(line["low_returns"], {"zone_1": 31.0, "zone_4": 33.0})
        self.assertEqual((line["air"], line["floor"]), (21.4, 21.0))
        self.assertAlmostEqual(line["power_w"], watts, places=1)
        self.assertAlmostEqual(line["q_kwh"], q_kwh, places=4)
        self.assertEqual(
            set(line),
            {"type", "ts", "zone", "call_start", "low_returns", "power_w", "air", "floor", "q_kwh"},
        )
        self.run_ticks(h, 4)
        self.assertEqual(len(h.log_lines("call_start")), 1)

    def test_a_loop_that_never_flowed_has_no_lowest_return(self):
        h = self.calling(21.4)
        self.run_ticks(h, 3, zone_4="unavailable")
        (line,) = h.log_lines("call_start")
        self.assertEqual(line["low_returns"], {"zone_1": 33.0, "zone_4": None})

    def test_a_call_that_ends_early_logs_its_stop_and_no_start(self):
        h = self.calling(21.4)
        self.run_ticks(h, 1)
        h.ha.set(FAMILY_DOOR, "on")
        h.tick(minutes=1)
        h.tick(minutes=2)
        (line,) = h.log_lines("call_stop")
        self.assertEqual((line["reason"], line["minutes_on"]), ("not_allowed", 8.0))
        self.assertEqual(line["flowing_zones"], ["family"])
        self.run_ticks(h, 6)
        self.assertEqual(h.log_lines("call_start"), [])

    def test_a_call_the_controller_did_not_see_begin_is_not_logged_as_a_start(self):
        h = self.calling(21.4)
        self.run_ticks(h, 1)
        restarted = h.restart()
        self.run_ticks(restarted, 4)
        self.assertEqual(restarted.attrs("family")["call"], "true")
        self.assertEqual(restarted.log_lines("call_start"), [])

    def test_nothing_is_counted_before_the_actuators_have_opened(self):
        h = self.calling(21.5)
        rt = h.app._zones["family"]
        self.water(h)
        h.tick(minutes=2)
        self.assertEqual(rt.transit.q_kwh, 0.0)
        self.assertEqual(h.attrs("family")["transit_power_w"], "0")
        h.tick(minutes=1)
        self.assertAlmostEqual(rt.transit.q_kwh, self.FAMILY_W * 60 / 3.6e6)

    def test_nothing_is_counted_while_the_pump_is_at_rest(self):
        for pump, counted in ((1.6, False), (15.0, False), (15.1, True)):
            with self.subTest(pump=pump):
                h = self.calling(21.5)
                self.run_ticks(h, 1, pump=pump)
                self.assertEqual(h.app._zones["family"].transit.q_kwh > 0, counted)

    def test_a_pump_that_cannot_be_read_counts_nothing(self):
        h = self.calling(21.5)
        self.water(h)
        h.ha.set(PUMP_POWER, "unavailable")
        h.tick(minutes=5)
        self.assertEqual(h.app._zones["family"].transit.q_kwh, 0.0)

    def test_a_probe_counts_while_it_reported_within_fifteen_minutes(self):
        h = self.calling(21.5)
        rt = h.app._zones["family"]
        self.water(h)
        h.ha.set(loop_probe("zone_4"), "33.0", updated=h.clock.now - timedelta(minutes=10))
        h.tick(minutes=5)
        self.assertAlmostEqual(rt.transit.q_kwh, self.STEP_KWH)
        before = rt.transit.q_kwh
        self.water(h)
        quiet = h.clock.now - timedelta(minutes=10, seconds=1)
        h.ha.set(loop_probe("zone_4"), "33.0", updated=quiet)
        h.tick(minutes=5)
        self.assertAlmostEqual(rt.transit.q_kwh, before * self.DECAY + self.LOOP_W * 300 / 3.6e6)

    def test_a_supply_that_has_gone_quiet_counts_nothing(self):
        h = self.calling(21.5)
        self.water(h)
        h.ha.set(SUPPLY_PROBE, "36.0", updated=h.clock.now - timedelta(minutes=11))
        h.tick(minutes=5)
        self.assertEqual(h.app._zones["family"].transit.q_kwh, 0.0)

    def test_a_probe_that_reads_nonsense_leaves_its_loop_out(self):
        for reading in ("unavailable", "unknown", "150.0", "-30.0", "n/a", "36.5"):
            with self.subTest(reading=reading):
                h = self.calling(21.5)
                self.run_ticks(h, 1, zone_4=reading)
                rt = h.app._zones["family"]
                self.assertAlmostEqual(rt.transit.q_kwh, self.LOOP_W * 300 / 3.6e6)

    def test_the_heat_leaves_once_the_call_has_stopped(self):
        h = self.stopped()
        rt = h.app._zones["family"]
        before = rt.transit.q_kwh
        self.run_ticks(h, 1)
        self.assertEqual(h.attrs("family")["call"], "false")
        self.assertAlmostEqual(rt.transit.q_kwh, before * self.DECAY)
        self.assertEqual(h.attrs("family")["transit_power_w"], "0")

    def test_a_restart_keeps_the_heat_in_transit(self):
        h = self.calling(21.5)
        self.run_ticks(h, 3)
        rt = h.app._zones["family"]
        saved = json.loads((h.tmp / "state.json").read_text())["zones"]["family"]["transit"]
        self.assertEqual(saved, {"q_kwh": rt.transit.q_kwh, "ts": h.clock.now.timestamp()})
        restarted = h.restart()
        again = restarted.app._zones["family"].transit
        self.assertEqual((again.q_kwh, again.ts), (rt.transit.q_kwh, h.clock.now.timestamp()))
        self.run_ticks(restarted, 1)
        self.assertAlmostEqual(again.q_kwh, self.kwh_after(4))

    def test_a_lost_state_starts_with_no_heat_in_transit(self):
        h = Harness(self, forecast=False)
        for rt in h.app._zones.values():
            self.assertEqual((rt.transit.q_kwh, rt.transit.ts), (0.0, None), rt.zc.name)
        for junk in (
            '{"zones": {"family": {"transit": "x"}}}',
            '{"zones": {"family": {"transit": {"q_kwh": "x"}}}}',
        ):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            args = shipped_args(tmp)
            Path(args["state_file"]).write_text(junk)
            h = Harness(self, args=args, forecast=False)
            h.tick()
            self.assertEqual(h.app._zones["family"].transit.q_kwh, 0.0, junk)
            self.assertEqual(h.warnings(), [], junk)

    def test_the_coast_is_never_more_than_half_a_degree(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        saved = {"zones": {"family": {"transit": {"q_kwh": 10.0, "ts": NOW.timestamp()}}}}
        Path(args["state_file"]).write_text(json.dumps(saved))
        h = Harness(self, args=args, forecast=False)
        h.tick()
        a = h.attrs("family")
        self.assertEqual((a["transit_q_kwh"], a["coast_c"]), ("10.000", "0.50"))

    def test_the_zone_sensor_publishes_the_heat_in_transit_as_text(self):
        h = self.calling(21.5)
        self.run_ticks(h, 1)
        for published in h.published:
            assert_publishable(self, published["attributes"])
        for zone in ZONES:
            for key in ("transit_power_w", "transit_q_kwh", "coast_c"):
                self.assertIsInstance(h.attrs(zone)[key], str, (zone, key))
        quiet = h.attrs("bathroom")
        self.assertEqual(
            (quiet["transit_power_w"], quiet["transit_q_kwh"], quiet["coast_c"]),
            ("0", "0.000", "0.00"),
        )

    def test_a_zone_without_a_transit_block_keeps_the_plain_rule(self):
        h = Harness(self, live_zones=["family"], zones=zones_without_family_transit())
        h.ha.set(h.app.cfg.live_switch_entity, "on")
        h.season()
        self.calling(harness=h)
        self.run_ticks(h, 8)
        a = h.attrs("family")
        self.assertEqual(a["call"], "true")
        for key in ("transit_power_w", "transit_q_kwh", "coast_c"):
            self.assertNotIn(key, a)
        self.assertIsNone(h.app._zones["family"].transit)
        saved = json.loads((h.tmp / "state.json").read_text())["zones"]["family"]
        self.assertNotIn("transit", saved)
        self.air(h, 21.75)
        self.run_ticks(h, 1)
        self.assertEqual(h.attrs("family")["call"], "false")
        self.assertEqual((h.log_lines("call_stop"), h.log_lines("call_start")), ([], []))


KRISTINE_WINDOW = "binary_sensor.kristines_room_window_contact"
FAMILY_DOOR = "binary_sensor.rooftop_door_1_contact"


class AiringLockoutEndToEnd(LiveZones, unittest.TestCase):
    def air_out(self, h, zone, opening, hours=3, cold=18.0):
        """Open `opening` at 12:00 in a warm room that cools to `cold`, close it `hours` later,
        and tick once more so the zone has seen the close. The opening is seen open at 12:01
        and closed at 12:01 plus `hours`."""
        h.ha.set(opening, "on")
        h.tick(minutes=1)
        h.tick(minutes=2)
        self.assertEqual(h.attrs(zone)["mode"], "window")
        self.air(h, cold, zone)
        h.echo()
        closed = datetime(2026, 10, 17, 12, 1, tzinfo=TZ) + timedelta(hours=hours)
        while closed - h.clock.now > timedelta(minutes=5):
            h.tick(minutes=5)
        h.tick(seconds=(closed - h.clock.now).total_seconds() - 60)
        h.ha.set(opening, "off")
        h.tick(minutes=1)
        self.assertEqual(h.clock.now, closed)
        self.assertEqual(h.attrs(zone)["mode"], "settle")
        return closed

    def test_a_three_hour_airing_in_a_residents_room_keeps_the_heat_off_for_three_hours(self):
        h = self.live("kristines_room")
        closed = self.air_out(h, "kristines_room", KRISTINE_WINDOW)
        deadline = closed + timedelta(hours=3)
        self.assertEqual(h.attrs("kristines_room")["heat_allowed_at"], deadline.isoformat())
        while h.clock.now + timedelta(minutes=5) < deadline:
            h.tick(minutes=5)
            a = h.attrs("kristines_room")
            self.assertEqual((a["mode"], a["call"]), ("settle", "false"), h.clock.now)
        self.assertEqual(self.sent(h), [5.0])
        h.tick(minutes=5)
        self.assertEqual(h.clock.now, deadline)
        a = h.attrs("kristines_room")
        self.assertEqual((a["mode"], a["call"]), ("hold", "true"))
        self.assertNotIn("heat_allowed_at", a)
        self.assertEqual(self.sent(h), [5.0, 22.5])

    def test_a_three_hour_airing_in_the_family_room_keeps_the_heat_off_for_three_hours(self):
        h = self.live("family")
        closed = self.air_out(h, "family", FAMILY_DOOR)
        deadline = closed + timedelta(hours=3)
        self.assertEqual(h.attrs("family")["heat_allowed_at"], deadline.isoformat())
        while h.clock.now + timedelta(minutes=5) < deadline:
            h.tick(minutes=5)
            self.assertEqual(h.attrs("family")["mode"], "settle")
        self.assertEqual(self.sent(h), [5.0])
        h.tick(minutes=5)
        self.assertEqual(h.clock.now, deadline)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["call"]), ("hold", "true"))
        self.assertEqual(self.sent(h), [5.0, 23.5])

    def test_every_zone_with_openings_waits_the_open_time_between_an_hour_and_four_hours(self):
        openings = {
            "family": FAMILY_DOOR,
            "kristines_room": KRISTINE_WINDOW,
            "claudias_room": "binary_sensor.rooftop_door_2_contact",
            "bathroom": "binary_sensor.bathroom_window_contact",
        }
        for zone, opening in openings.items():
            for hours, wait in ((20 / 60, 1), (3, 3), (6, 4)):
                with self.subTest(zone=zone, hours=hours):
                    h = self.live(zone)
                    closed = self.air_out(h, zone, opening, hours=hours)
                    deadline = closed + timedelta(hours=wait)
                    self.assertEqual(h.attrs(zone)["heat_allowed_at"], deadline.isoformat())

    def test_heat_waits_for_the_next_slot_when_the_lockout_ends_between_two(self):
        h = self.live("kristines_room")
        closed = self.air_out(h, "kristines_room", KRISTINE_WINDOW, hours=3.4)
        deadline = closed + timedelta(hours=3.4)
        self.assertEqual(deadline, datetime(2026, 10, 17, 18, 49, tzinfo=TZ))
        while deadline - h.clock.now > timedelta(minutes=5):
            h.tick(minutes=5)
        h.tick(minutes=5)
        a = h.attrs("kristines_room")
        self.assertEqual((h.clock.now, a["mode"], a["call"]), (deadline + timedelta(minutes=1), "hold", "false"))
        self.assertEqual(h.app._zones["kristines_room"].call_reason, "wait_slot")
        self.assertEqual(self.sent(h), [5.0])
        while h.clock.now < datetime(2026, 10, 17, 19, 0, tzinfo=TZ):
            h.tick(minutes=5)
        a = h.attrs("kristines_room")
        self.assertEqual((h.clock.now.hour, h.clock.now.minute < 6), (19, True))
        self.assertEqual((a["call"], a["call_since"]), ("true", h.clock.now.isoformat()))
        self.assertEqual(self.sent(h), [5.0, 22.5])

    def test_a_room_that_has_recovered_ends_the_lockout_early(self):
        h = self.live("kristines_room")
        self.air_out(h, "kristines_room", KRISTINE_WINDOW)
        self.air(h, 22.0, "kristines_room")
        h.tick(minutes=5)
        a = h.attrs("kristines_room")
        self.assertEqual((a["mode"], a["call"]), ("hold", "false"))
        self.assertNotIn("heat_allowed_at", a)
        self.assertEqual(self.sent(h), [5.0])

    def test_a_restart_in_the_lockout_keeps_its_deadline(self):
        h = self.live("kristines_room")
        closed = self.air_out(h, "kristines_room", KRISTINE_WINDOW)
        deadline = closed + timedelta(hours=3)
        h.tick(minutes=90)
        restarted = h.restart()
        restarted.tick()
        a = restarted.attrs("kristines_room")
        self.assertEqual((a["mode"], a["heat_allowed_at"]), ("settle", deadline.isoformat()))
        while restarted.clock.now + timedelta(minutes=5) < deadline:
            restarted.tick(minutes=5)
        self.assertEqual(restarted.calls, [])
        restarted.tick(minutes=5)
        self.assertEqual(restarted.clock.now, deadline)
        self.assertEqual(restarted.attrs("kristines_room")["call"], "true")
        self.assertEqual(self.sent(restarted), [22.5])

    def test_a_residents_app_setpoint_in_the_lockout_is_restored_to_the_minimum(self):
        h = self.live("kristines_room")
        self.air_out(h, "kristines_room", KRISTINE_WINDOW, hours=1)
        h.tick(minutes=10)
        h.service_call("kristines_room", "set_temperature", KRISTINE_ID, temperature=24.0)
        h.report("kristines_room", temperature=24.0)
        self.assertEqual(h.effects("kristines_room")[-1], "restore")
        rt = h.app._zones["kristines_room"]
        self.assertEqual(rt.nudge, 0.0)
        h.tick(seconds=1)
        self.assertEqual(self.sent(h), [5.0, 5.0])
        self.assertEqual(h.attrs("kristines_room")["mode"], "settle")

    def test_a_reopening_in_the_lockout_counts_from_the_first_opening(self):
        h = self.live("kristines_room")
        self.air_out(h, "kristines_room", KRISTINE_WINDOW, hours=2)
        h.ha.set(KRISTINE_WINDOW, "on")
        h.tick(minutes=1)
        h.tick(minutes=2)
        self.assertEqual(h.attrs("kristines_room")["mode"], "window")
        h.ha.set(KRISTINE_WINDOW, "off")
        h.tick(minutes=1)
        reclosed = h.clock.now
        a = h.attrs("kristines_room")
        self.assertEqual(a["mode"], "settle")
        opened = datetime(2026, 10, 17, 12, 1, tzinfo=TZ)
        self.assertEqual(a["heat_allowed_at"], (reclosed + (reclosed - opened)).isoformat())


class OffSeasonPlan(unittest.TestCase):
    def test_all_sensors_appear_and_the_plan_is_computed_while_the_season_is_off(self):
        h = Harness(self)
        h.tick()
        entities = {p["entity"] for p in h.published}
        self.assertEqual(
            entities,
            {f"sensor.heating_zone_{z}" for z in ZONES} | {"sensor.heating_controller"},
        )
        fam = h.zone("family")
        self.assertEqual(fam["state"], 21.5)
        self.assertEqual(fam["attributes"]["mode"], "off_season")
        self.assertEqual(fam["attributes"]["plan_mode"], "hold")
        self.assertEqual(fam["attributes"]["setpoint_now"], 21.5)
        self.assertEqual(fam["attributes"]["call"], "false")
        self.assertEqual(h.last("sensor.heating_controller")["state"], "off_season")

    def test_published_attributes_survive_set_state(self):
        h = Harness(self)
        h.tick()
        for p in h.published:
            self.assertIs(p["replace"], True, p["entity"])
            assert_publishable(self, p["attributes"])

    def test_the_decided_numbers_show_up_per_zone(self):
        h = Harness(self)
        h.tick()
        # 12:00: the family target is the base, the others are flat, the bathroom holds its day level
        want = {
            "family": (21.5, 21.5),
            "kristines_room": (20.5, 20.5),
            "claudias_room": (21.5, 21.5),
            "bathroom": (20.0, 20.0),
            "guest_bathroom": (20.0, 20.0),
        }
        for zone, (state, setpoint) in want.items():
            self.assertEqual(h.zone(zone)["state"], state, zone)
            self.assertEqual(h.attrs(zone)["setpoint_now"], setpoint, zone)

    def test_the_first_plan_has_a_timeline_and_a_next_change(self):
        h = Harness(self, zones=zones_with_family_night_offset())
        h.tick()
        a = h.attrs("family")
        self.assertEqual(
            [(b["start"], b["end"], b["setpoint"]) for b in a["day_blocks"]],
            [("12:00", "19:00", "21.5"), ("19:00", "24:00", "21.0")],
        )
        self.assertEqual(a["next_change_at"], "2026-10-17T19:00:00+02:00")
        self.assertEqual(a["next_setpoint"], 21.0)
        for gone in ("trim", "weather_offset"):
            self.assertNotIn(gone, a)
        self.assertEqual(a["nudge"], "+0.0")
        self.assertEqual(a["lower_limit"], 19.0)
        self.assertEqual(a["live"], "false")

    def test_controller_sensor(self):
        h = Harness(self)
        h.tick()
        c = h.last("sensor.heating_controller")["attributes"]
        self.assertEqual(c["zones_live"], ["<none>"])
        self.assertEqual(c["forecast_source"], "open-meteo")
        self.assertEqual(c["forecast_ok"], "true")
        self.assertEqual(c["outdoor_source"], "sensor.gw2000a_outdoor_temperature")
        self.assertEqual(c["outdoor_now"], "11.3")
        self.assertEqual(c["last_tick"], "2026-10-17T12:00:00+02:00")
        self.assertEqual(c["sun_term_factor"], 0.5)


class InSeasonPlan(unittest.TestCase):
    def test_season_on_turns_the_plan_into_modes(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        self.assertEqual(h.last("sensor.heating_controller")["state"], "shadow")
        for zone in ZONES:
            self.assertEqual(h.attrs(zone)["mode"], "hold", zone)
        self.assertEqual(h.attrs("family")["setpoint_now"], 21.5)

    def test_a_helper_change_moves_the_target_and_counts_as_a_priority_event(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        h.ha.set("input_number.heating_target_family", "22.0")
        h.tick(minutes=1)
        self.assertEqual(h.zone("family")["state"], 22.0)
        d = [r for r in h.log_lines("decision") if r["zone"] == "family"][-1]
        self.assertEqual(d["gate"], "priority:target_change")
        self.assertTrue(d["would_write"])

    def test_an_unreadable_helper_falls_back_to_the_yaml_default(self):
        h = Harness(self)
        h.ha.set("input_number.heating_target_family", "unavailable")
        h.tick()
        self.assertEqual(h.zone("family")["state"], 21.5)

    def test_thermostat_offline_gates_the_zone_but_still_plans(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.ha.set(THERMOSTATS["kristines_room"], "unavailable")
        h.tick()
        a = h.attrs("kristines_room")
        self.assertEqual(
            (a["mode"], a["plan_mode"], a["setpoint_now"]), ("device_offline", "hold", 20.5)
        )
        self.assertTrue(any("thermostat:" in s for s in a["stale_inputs"]))
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_one_zone_failing_does_not_stop_the_others(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        original = h.app._read_zone

        def boom(rt, now_ts, window_active):
            if rt.zc.name == "bathroom":
                raise RuntimeError("boom")
            return original(rt, now_ts, window_active)

        h.app._read_zone = boom
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")
        self.assertTrue(any(lvl == "ERROR" and "bathroom" in m for lvl, m in h.logs))
        h.last("sensor.heating_controller")


class ControlAirChain(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.ha.set("input_boolean.heating_season", "on")

    def test_room_readings_are_the_normal_source(self):
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["control_air"], a["control_air_source"]), (22.2, "feel"))

    def test_the_family_air_is_the_living_room_alone(self):
        self.h.ha.set("sensor.living_room_feel", "22.0", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "25.0", {"stale": "false"})
        self.h.ha.set("sensor.kitchen_feel", "20.0", {"stale": "false"})
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["control_air"], a["control_air_source"]), (22.0, "feel"))

    def test_the_living_room_alone_steers_the_family_call(self):
        self.h.ha.set("sensor.living_room_feel", "20.0", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "23.0", {"stale": "false"})
        self.h.ha.set("sensor.kitchen_feel", "23.0", {"stale": "false"})
        self.h.tick()
        self.assertEqual(self.h.attrs("family")["call"], "true")
        self.h.ha.set("sensor.living_room_feel", "22.5", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "19.0", {"stale": "false"})
        self.h.ha.set("sensor.kitchen_feel", "19.0", {"stale": "false"})
        self.h.tick(minutes=60)
        self.assertEqual(self.h.attrs("family")["call"], "false")

    def test_a_zone_with_one_room_is_not_weighted_or_given_a_basis(self):
        self.h.tick()
        for zone in ZONES:
            self.assertNotIn("control_air_basis", self.h.attrs(zone), zone)
        self.assertEqual(self.h.attrs("kristines_room")["control_air"], 22.2)

    def test_a_stale_room_reading_falls_back_to_the_thermostat_itself(self):
        for feel in FEELS["family"]:
            self.h.ha.set(feel, "22.2", {"stale": "true"})
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["control_air"], a["control_air_source"]), (22.8, "thermostat"))
        self.assertEqual(a["mode"], "hold")

    def test_then_the_floor_sensor_plus_half_a_degree(self):
        for feel in FEELS["family"]:
            self.h.ha.set(feel, "unavailable")
        old = self.h.clock.now - timedelta(minutes=200)
        self.h.ha.set(
            THERMOSTATS["family"],
            "unavailable",
            {"current_temperature": 22.8, "temperature": 15.0},
            updated=old,
        )
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["control_air"], a["control_air_source"]), (21.5, "floor"))

    def test_with_nothing_usable_the_zone_goes_to_fallback(self):
        for feel in FEELS["family"]:
            self.h.ha.set(feel, "unavailable")
        old = self.h.clock.now - timedelta(minutes=200)
        self.h.ha.set(
            THERMOSTATS["family"],
            "heat",
            {"current_temperature": None, "temperature": 15.0},
            updated=old,
        )
        for floor in (LIVING_FLOOR, DINING_FLOOR):
            self.h.ha.set(floor, "21.0", updated=old)
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["mode"], a["control_air_source"]), ("fallback", "none"))
        self.assertEqual(a["setpoint_now"], 21.5)
        self.assertEqual(a["call"], "false")
        self.assertTrue(a["stale_inputs"])

    def test_normal_heartbeat_and_unchanged_online_state_are_usable(self):
        for age_min in (60.0, 65.6, 131.2, 225.0, 24 * 60.0):
            with self.subTest(age_min=age_min):
                old = self.h.clock.now - timedelta(minutes=age_min)
                self.h.ha.set(
                    THERMOSTATS["family"], "heat",
                    {"current_temperature": 22.8, "temperature": 15.0}, updated=old,
                )
                # An HA state_changed snapshot can also carry an old last_reported;
                # unchanged reports do not update AppDaemon's cached snapshot.
                self.h.ha.entities[THERMOSTATS["family"]]["last_reported"] = old.isoformat()
                self.h.tick()
                attrs = self.h.attrs("family")
                self.assertNotIn(
                    f"thermostat_reading:{THERMOSTATS['family']}", attrs["stale_inputs"]
                )
                self.assertEqual(attrs["thermostat_freshness_source"], "availability")
                self.assertEqual(
                    datetime.fromisoformat(attrs["thermostat_state_updated_at"]), old
                )

    def test_unchanged_online_temperature_is_a_usable_fallback(self):
        old = self.h.clock.now - timedelta(hours=6)
        self.h.ha.set(
            THERMOSTATS["family"], "heat",
            {"current_temperature": 22.8, "temperature": 15.0}, updated=old,
        )
        app, rt = self.h.app, self.h.app._zones["family"]
        now_ts = self.h.clock.now.timestamp()
        reading = app._read_zone(rt, now_ts, False)
        self.assertEqual((reading["air"], reading["air_source"]), (22.2, "feel"))
        for feel in FEELS["family"]:
            self.h.ha.set(feel, "unavailable")
        reading = app._read_zone(rt, now_ts, False)
        self.assertEqual((reading["air"], reading["air_source"]), (22.8, "thermostat"))
        self.assertTrue(reading["air_fresh"])

    def test_missing_state_timestamp_is_unknown_age_not_a_stale_thermostat(self):
        self.h.ha.entities[THERMOSTATS["family"]].pop("last_updated")
        self.h.tick()
        attrs = self.h.attrs("family")
        self.assertNotIn("thermostat_state_updated_at", attrs)
        self.assertNotIn(f"thermostat_reading:{THERMOSTATS['family']}", attrs["stale_inputs"])

    def test_lost_device_with_cached_temperature_is_not_a_reading(self):
        old = self.h.clock.now - timedelta(minutes=200)
        self.h.ha.set(
            THERMOSTATS["family"], "unavailable",
            {"current_temperature": 22.8, "temperature": 15.0}, updated=old,
        )
        app, rt = self.h.app, self.h.app._zones["family"]
        now_ts = self.h.clock.now.timestamp()
        reading = app._read_zone(rt, now_ts, False)
        self.assertIsNone(reading["therm_temp"])
        self.assertFalse(reading["therm_usable"])
        self.assertIn(f"thermostat:{THERMOSTATS['family']}", reading["stale"])

    def test_online_invalid_temperature_is_still_rejected(self):
        for value in (None, "unknown", "unavailable", "nan", "inf", -20, 88):
            with self.subTest(value=value):
                self.h.ha.set(
                    THERMOSTATS["family"], "heat",
                    {"current_temperature": value, "temperature": 15.0},
                )
                self.h.tick()
                attrs = self.h.attrs("family")
                self.assertIn(f"thermostat_reading:{THERMOSTATS['family']}", attrs["stale_inputs"])
                self.assertEqual(attrs["control_air_source"], "feel")

    def test_floor_fallback_freshness_is_not_widened_to_fit_salus(self):
        for feel in FEELS["family"]:
            self.h.ha.set(feel, "unavailable")
        self.h.ha.set(THERMOSTATS["family"], "unavailable")
        for floor in (LIVING_FLOOR, DINING_FLOOR):
            self.h.ha.set(floor, "21.0", updated=self.h.clock.now - timedelta(minutes=65.6))
        reading = self.h.app._read_zone(
            self.h.app._zones["family"], self.h.clock.now.timestamp(), False
        )
        self.assertEqual(reading["air_source"], "floor")
        self.assertFalse(reading["air_fresh"])

    def test_an_impossible_reading_is_ignored(self):
        self.h.ha.set("sensor.living_room_feel", "88.0", {"stale": "false"})
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["control_air"], a["control_air_source"]), (22.8, "thermostat"))

    def test_a_jump_inside_ten_minutes_is_held_back(self):
        self.h.tick()
        for feel in FEELS["family"]:
            self.h.ha.set(feel, "26.5", {"stale": "false"})
        self.h.tick(minutes=5)
        self.assertEqual(self.h.attrs("family")["control_air"], 22.2)
        self.h.tick(minutes=5)
        self.assertEqual(self.h.attrs("family")["control_air"], 26.5)


class ControlAirOfSeveralRooms(unittest.TestCase):
    """No shipped zone reads more than one room; a zone that is given several averages them by
    weight."""

    def setUp(self):
        self.h = Harness(self, zones=zones_with_several_family_rooms())
        self.h.ha.set("input_boolean.heating_season", "on")

    def test_the_air_is_the_mean_of_the_rooms_by_weight(self):
        self.h.ha.set("sensor.living_room_feel", "26.0", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "22.0", {"stale": "false"})
        self.h.ha.set("sensor.kitchen_feel", "22.0", {"stale": "false"})
        self.h.tick()
        a = self.h.attrs("family")
        # 23.357 by weight, 23.333 unweighted
        self.assertEqual((a["control_air"], a["control_air_basis"]), (23.4, "weighted"))
        self.assertNotIn("control_air_basis", self.h.attrs("kristines_room"))

    def test_a_room_that_does_not_read_drops_out_of_the_weighted_mean(self):
        app, rt = self.h.app, self.h.app._zones["family"]
        now_ts = self.h.clock.now.timestamp()
        unreadable = (("18.0", {"stale": "true"}), ("unavailable", {}), ("88.0", {"stale": "false"}))
        for kitchen in unreadable:
            with self.subTest(kitchen=kitchen):
                self.h.ha.set("sensor.living_room_feel", "26.0", {"stale": "false"})
                self.h.ha.set("sensor.dining_room_feel", "22.0", {"stale": "false"})
                self.h.ha.set("sensor.kitchen_feel", *kitchen)
                reading = app._read_zone(rt, now_ts, False)
                self.assertAlmostEqual(reading["air"], (19 * 26.0 + 18.5 * 22.0) / 37.5)
                self.assertEqual((reading["air_source"], reading["air_basis"]), ("feel", "weighted"))
                self.assertEqual(reading["stale"], ["control_air:sensor.kitchen_feel"])

    def test_one_room_that_reads_is_enough(self):
        self.h.ha.set("sensor.dining_room_feel", "unavailable")
        self.h.ha.set("sensor.kitchen_feel", "22.0", {"stale": "true"})
        self.h.ha.set("sensor.living_room_feel", "22.6", {"stale": "false"})
        self.h.tick()
        self.assertEqual(self.h.attrs("family")["control_air"], 22.6)
        self.assertEqual(
            self.h.attrs("family")["stale_inputs"],
            ["control_air:sensor.dining_room_feel", "control_air:sensor.kitchen_feel"],
        )


class DeviceLimits(unittest.TestCase):
    def test_matching_limits_stay_quiet(self):
        h = Harness(self)
        h.tick()
        self.assertEqual(h.warnings(), [])

    def test_the_factory_range_fits_the_drive_and_stays_quiet(self):
        h = Harness(self)
        for thermostat in THERMOSTATS.values():
            stem = thermostat.split(".")[1]
            h.ha.set(f"number.{stem}_min_heat_setpoint", 5)
            h.ha.set(f"number.{stem}_max_heat_setpoint", 35)
        h.tick()
        h.tick(minutes=5)
        self.assertEqual(h.warnings(), [])
        self.assertEqual(h.attrs("family")["upper_limit"], 24.0)  # the configured limits still apply

    def test_a_minimum_the_drive_cannot_reach_is_reported_once_per_thermostat(self):
        h = Harness(self)
        for thermostat in THERMOSTATS.values():
            stem = thermostat.split(".")[1]
            h.ha.set(f"number.{stem}_min_heat_setpoint", 7)
            h.ha.set(f"number.{stem}_max_heat_setpoint", 35)
        h.tick()
        h.tick(minutes=5)
        found = [m for m in h.warnings() if "thermostat limits" in m]
        self.assertEqual(len(found), 5)
        self.assertIn("7.0-35.0 do not fit the 5.0-24.0 the controller writes", found[0].replace("7-35", "7.0-35.0"))

    def test_a_maximum_under_the_zones_upper_limit_is_reported(self):
        h = Harness(self)
        h.ha.set("number.family_room_thermostat_max_heat_setpoint", 22)
        h.tick()
        found = [m for m in h.warnings() if "thermostat limits" in m]
        self.assertEqual(len(found), 1)
        self.assertIn("family", found[0])

    def test_a_stricter_device_limit_wins(self):
        h = Harness(self)
        h.ha.set("number.bathroom_thermostat_min_heat_setpoint", 20.5)
        h.ha.set("number.family_room_thermostat_max_heat_setpoint", 22.0)
        h.ha.set("input_number.heating_target_family", "23.0")
        h.tick()
        self.assertEqual(h.attrs("bathroom")["lower_limit"], 20.5)
        self.assertEqual(h.attrs("bathroom")["setpoint_now"], 20.5)
        self.assertEqual(h.attrs("family")["upper_limit"], 22.0)
        self.assertEqual(h.attrs("family")["setpoint_now"], 22.0)


class ForecastHandling(unittest.TestCase):
    def test_the_location_comes_from_zone_home_rounded_and_is_never_logged(self):
        h = Harness(self)
        self.assertEqual(len(h.urls), 1)
        self.assertIn("latitude=55.68&longitude=12.57&", h.urls[0])
        h.tick()
        blob = json.dumps(h.published, default=str) + json.dumps(h.logs)
        blob += "".join(f.read_text() for f in (h.tmp / "heating_log").glob("*"))
        blob += (h.tmp / "state.json").read_text()
        for needle in (str(HOME_LAT), str(HOME_LON), "55.68", "12.57", "latitude", "longitude"):
            self.assertNotIn(needle, blob)

    def test_each_fetch_is_logged_with_48_hours_and_no_location(self):
        h = Harness(self)
        line = h.log_lines("forecast")[0]
        for key in ("temp", "wind", "ghi", "cloud"):
            self.assertEqual(len(line[key]), 48, key)
        self.assertEqual(line["step_s"], 3600)
        self.assertEqual(line["ts"], "2026-10-17T12:00:00+02:00")
        self.assertNotIn("latitude", json.dumps(line))
        # the first logged hour is the one that ends at 12:00
        self.assertEqual(datetime.fromtimestamp(line["t0"], TZ).hour, 12)

    def test_a_failed_fetch_keeps_the_last_good_forecast_and_warns_once(self):
        h = Harness(self)
        h.fetch_error = OSError("no route")
        h.at(13)
        h.fetch()
        h.at(14)
        h.fetch()
        self.assertEqual(len([m for m in h.warnings() if "forecast fetch failed" in m]), 1)
        h.tick()
        self.assertEqual(h.last("sensor.heating_controller")["attributes"]["forecast_ok"], "true")
        h.fetch_error = None
        h.fetch()
        self.assertTrue(any("works again" in m for _l, m in h.logs))

    def test_junk_in_the_response_counts_as_a_failed_fetch(self):
        h = Harness(self, forecast=False)
        h.payload = {"error": True}
        h.fetch()
        self.assertTrue(any("forecast fetch failed" in m for m in h.warnings()))
        h.tick()
        self.assertEqual(h.last("sensor.heating_controller")["attributes"]["forecast_ok"], "false")

    def test_a_stale_forecast_stops_the_sun_cuts(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(2, 55, day=18)
        h.fetch()
        h.at(8, 0, day=18)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "sun_cut")
        h.fetch_error = OSError("down")
        h.at(15, 0, day=18)
        h.tick()
        self.assertEqual(h.last("sensor.heating_controller")["attributes"]["forecast_ok"], "false")
        h.at(8, 0, day=19)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")
        self.assertTrue(any("forecast stale" in m for m in h.warnings()))

    def test_no_forecast_at_all_means_no_budget_and_no_cut(self):
        h = Harness(self, forecast=False)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(3, 0, day=18)
        h.tick()
        a = h.attrs("family")
        self.assertEqual(a["mode"], "hold")
        self.assertNotIn("budget_today_h", a)

    def test_a_missing_location_skips_the_fetch(self):
        h = Harness(self, forecast=False)
        h.ha.remove("zone.home")
        h.fetch()
        self.assertEqual(h.urls, [])
        self.assertTrue(any("no location" in m for m in h.warnings()))


class FloorCapEndToEnd(unittest.TestCase):
    def hot_floor(self, living="24.5", dining="21.0"):
        h = Harness(self, forecast=False)
        h.ha.set(LIVING_FLOOR, living)
        h.ha.set(DINING_FLOOR, dining)
        h.season()
        return h

    def test_a_hot_floor_caps_the_family_room_and_publishes_the_setting(self):
        h = self.hot_floor()
        a = h.attrs("family")
        self.assertEqual(
            (a["mode"], a["setpoint_now"], a["floor_max_c"]), ("floor_cap", 21.5, 24.5)
        )
        self.assertEqual(a["call"], "false")
        self.assertNotIn("weather_offset", a)
        self.assertIn("until the floor is below 24.0", a["reasons"][0])
        self.assertEqual(h.attrs("kristines_room")["mode"], "hold")
        records = {r["zone"]: r for r in h.log_lines("decision")}
        self.assertEqual(records["family"]["plan_mode"], "floor_cap")
        self.assertEqual(records["family"]["call_reason"], "not_allowed")
        self.assertEqual(records["kristines_room"]["plan_mode"], "hold")

    def test_the_warmer_of_the_two_floors_decides_whichever_it_is(self):
        for living, dining in (("24.5", "21.0"), ("21.0", "24.5"), ("24.6", "24.5")):
            with self.subTest(living=living, dining=dining):
                h = self.hot_floor(living, dining)
                self.assertEqual(h.attrs("family")["mode"], "floor_cap")
        for living, dining in (("24.4", "24.4"), ("21.0", "24.49"), ("23.9", "21.0")):
            with self.subTest(living=living, dining=dining):
                h = self.hot_floor(living, dining)
                self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_both_floors_are_published_with_the_warmer_as_the_floor_temperature(self):
        h = self.hot_floor("23.5", "24.0")
        a = h.attrs("family")
        self.assertEqual(a["floor_temp"], 24.0)
        self.assertEqual(a["floor_temps"], {LIVING_FLOOR: 23.5, DINING_FLOOR: 24.0})
        d = [r for r in h.log_lines("decision") if r["zone"] == "family"][-1]
        self.assertEqual(d["floor"], 24.0)
        self.assertEqual(d["floors"], {LIVING_FLOOR: 23.5, DINING_FLOOR: 24.0})
        self.assertNotIn("floor_temps", h.attrs("kristines_room"))
        for published in h.published:
            assert_publishable(self, published["attributes"])

    def test_the_cap_releases_only_when_the_warmer_floor_is_below_the_boundary(self):
        h = self.hot_floor("24.5", "24.5")
        h.ha.set(LIVING_FLOOR, "23.5")
        h.ha.set(DINING_FLOOR, "24.0")
        h.tick(minutes=5)
        self.assertEqual(h.attrs("family")["mode"], "floor_cap")
        h.ha.set(DINING_FLOOR, "23.99")
        h.tick(minutes=5)
        self.assertEqual(h.attrs("family")["mode"], "hold")
        h.ha.set(LIVING_FLOOR, "24.4")
        h.tick(minutes=5)
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_hysteresis_survives_restart_and_releases_below_the_boundary(self):
        h = self.hot_floor()
        h.ha.set(LIVING_FLOOR, "24.0")
        h.tick(minutes=5)
        self.assertEqual(h.attrs("family")["mode"], "floor_cap")
        saved = json.loads((h.tmp / "state.json").read_text())
        self.assertIs(saved["zones"]["family"]["floor_cap"], True)
        restarted = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        self.assertTrue(restarted.app._zones["family"].floor_cap)
        restarted.ha.entities = copy.deepcopy(h.ha.entities)
        restarted.tick()
        self.assertEqual(restarted.attrs("family")["mode"], "floor_cap")
        restarted.ha.set(LIVING_FLOOR, "23.99")
        restarted.tick(minutes=5)
        self.assertFalse(restarted.app._zones["family"].floor_cap)
        self.assertEqual(restarted.attrs("family")["mode"], "hold")
        restarted.ha.set(LIVING_FLOOR, "24.4")
        restarted.tick(minutes=5)
        self.assertEqual(restarted.attrs("family")["mode"], "hold")

    def test_stale_floor_suspends_the_cap_without_erasing_its_latch(self):
        h = self.hot_floor()
        h.tick(minutes=1440)
        self.assertEqual(h.attrs("family")["mode"], "floor_cap")
        h.tick(seconds=1)
        self.assertEqual(h.attrs("family")["mode"], "hold")
        self.assertTrue(h.app._zones["family"].floor_cap)
        h.ha.set(LIVING_FLOOR, "24.2")
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "floor_cap")
        h.ha.remove(LIVING_FLOOR)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")
        self.assertTrue(h.app._zones["family"].floor_cap)
        h.ha.set(LIVING_FLOOR, "23.9")
        h.tick()
        self.assertFalse(h.app._zones["family"].floor_cap)
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_each_floor_counts_for_the_cap_for_a_day_on_its_own(self):
        for age, mode in ((1440.0, "floor_cap"), (1440.01, "hold")):
            with self.subTest(age=age):
                h = Harness(self, forecast=False)
                h.ha.set(DINING_FLOOR, "24.6", updated=h.clock.now - timedelta(minutes=age))
                h.season()
                self.assertEqual(h.attrs("family")["mode"], mode)
                self.assertEqual(h.attrs("family")["floor_temp"], 24.6)

    def test_a_fresh_hot_floor_counts_beside_a_stale_cool_one(self):
        h = Harness(self, forecast=False)
        h.ha.set(DINING_FLOOR, "21.0", updated=h.clock.now - timedelta(minutes=3000))
        h.ha.set(LIVING_FLOOR, "24.5")
        h.season()
        self.assertEqual(h.attrs("family")["mode"], "floor_cap")

    def test_a_floor_that_does_not_read_leaves_the_other_in_charge(self):
        for broken in (None, "unavailable", "nan"):
            with self.subTest(broken=broken):
                h = Harness(self, forecast=False)
                h.ha.set(DINING_FLOOR, "24.5")
                if broken is None:
                    h.ha.remove(LIVING_FLOOR)
                else:
                    h.ha.set(LIVING_FLOOR, broken)
                h.season()
                a = h.attrs("family")
                self.assertEqual((a["mode"], a["floor_temp"]), ("floor_cap", 24.5))
                self.assertEqual(a["floor_temps"], {DINING_FLOOR: 24.5})
                self.assertIn(f"floor:{LIVING_FLOOR}", a["stale_inputs"])

    def test_missing_invalid_undated_and_stale_floors_do_not_enter_the_cap(self):
        readings = (
            (None, 0), ("unavailable", 0), ("nan", 0), ("24.5", 1440.01), ("24.5", None)
        )
        for value, age in readings:
            with self.subTest(value=value, age=age):
                h = Harness(self, forecast=False)
                for floor in (LIVING_FLOOR, DINING_FLOOR):
                    if value is None:
                        h.ha.remove(floor)
                    else:
                        h.ha.set(floor, value, updated=h.clock.now - timedelta(minutes=age or 0))
                        if age is None:
                            h.ha.entities[floor].pop("last_updated")
                h.season()
                self.assertFalse(h.app._zones["family"].floor_cap)
                self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_runtime_without_saved_cap_state_is_backwards_compatible(self):
        h = Harness(self, forecast=False)
        zc = h.app.cfg.zones["family"]
        for saved in ({}, {"floor_cap": "true"}, {"floor_cap": []}):
            with self.subTest(saved=saved):
                rt = hc.ZoneRuntime(zc, h.app.cfg, saved)
                self.assertFalse(rt.floor_cap)

    def test_window_wins_and_retains_the_cap_for_when_it_closes(self):
        h = self.hot_floor()
        h.ha.set("binary_sensor.living_room_window_contact", "on")
        h.tick(minutes=1)
        h.tick(minutes=2)
        self.assertEqual(h.attrs("family")["mode"], "window")
        self.assertTrue(h.app._zones["family"].floor_cap)
        h.ha.set("binary_sensor.living_room_window_contact", "off")
        h.tick(minutes=1)
        self.assertEqual(h.attrs("family")["mode"], "settle")
        h.tick(minutes=1)
        self.assertEqual(h.attrs("family")["mode"], "floor_cap")

    def test_a_zone_with_one_floor_is_capped_by_it_and_publishes_no_list(self):
        h = Harness(self, forecast=False)
        h.ha.set("sensor.claudias_room_floor_thermometer_temperature", "24.5")
        h.season()
        a = h.attrs("claudias_room")
        self.assertEqual((a["mode"], a["floor_temp"]), ("floor_cap", 24.5))
        self.assertNotIn("floor_temps", a)
        self.assertEqual(h.attrs("family")["mode"], "hold")


class SunCut(unittest.TestCase):
    def season(self, **kw):
        h = Harness(self, **kw)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(2, 55, day=18)
        h.fetch()
        return h

    def test_the_sunny_day_cuts_the_family_zone_from_two_hours_before_the_sun(self):
        h = self.season()
        h.at(8, 0, day=18)
        h.tick()
        a = h.attrs("family")
        self.assertEqual(a["mode"], "sun_cut")
        self.assertEqual(a["setpoint_now"], 21.0)  # the protected minimum, base less half a degree
        self.assertEqual(
            a["cut_window"], ["2026-10-18T07:00:00+02:00", "2026-10-18T16:00:00+02:00"]
        )
        self.assertIn("Sun expected until 16:00", a["reasons"][0])
        self.assertEqual(a["sun_credit_h"], "2.0")
        self.assertEqual(a["budget_today_h"], "1.6")
        self.assertEqual(a["budget_dull_h"], "5.7")

    def test_the_cut_ends_with_its_window(self):
        h = self.season()
        h.at(15, 55, day=18)
        h.fetch()
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "sun_cut")
        h.at(16, 5, day=18)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_before_the_window_the_cut_is_announced_but_not_running(self):
        h = self.season()
        h.at(6, 30, day=18)
        h.tick()
        a = h.attrs("family")
        self.assertEqual(a["mode"], "hold")
        self.assertEqual(a["cut_window"][0], "2026-10-18T07:00:00+02:00")

    def test_the_dull_day_is_not_cut(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(10, 0)  # inside the window that today's one bright hour would open
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")
        # one bright hour is no sun day; the window shown is tomorrow's
        self.assertEqual(h.attrs("family")["cut_window"][0], "2026-10-18T07:00:00+02:00")

    def test_the_other_zones_are_never_cut(self):
        h = self.season()
        h.at(10, 0, day=18)
        h.tick()
        for zone in ("kristines_room", "claudias_room", "bathroom", "guest_bathroom"):
            self.assertNotEqual(h.attrs(zone)["mode"], "sun_cut")

    def test_more_trust_in_the_sun_cuts_a_sunny_day_that_less_trust_leaves_alone(self):
        h = Harness(self, sun_term_factor=0.05)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(7, 55, day=18)
        h.fetch()
        h.at(8, 0, day=18)
        h.tick()  # the credit covers less than credit_ratio of the heat skipped
        self.assertEqual(h.attrs("family")["mode"], "hold")
        h.app.cfg = dataclasses.replace(h.app.cfg, sun_term_factor=0.1)  # and now more than that
        h.tick(minutes=5)
        self.assertEqual(h.attrs("family")["mode"], "sun_cut")


class WindowEndToEnd(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.h.ha.set("input_boolean.heating_season", "on")
        self.h.tick()

    def open_door(self):
        self.h.ha.set("binary_sensor.rooftop_door_1_contact", "on")

    def test_an_airing_pauses_the_zone_then_settles_then_restores(self):
        h = self.h
        self.open_door()
        h.tick(minutes=1)
        self.assertEqual(h.attrs("family")["mode"], "hold")  # not two minutes yet
        h.tick(minutes=2)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["setpoint_now"], a["call"]), ("window", 21.5, "false"))
        d = [r for r in h.log_lines("decision") if r["zone"] == "family"][-1]
        self.assertEqual(
            (d["gate"], d["would_write"], d["drive"]), ("priority:window_open", True, 5.0)
        )
        h.ha.set("binary_sensor.rooftop_door_1_contact", "off")
        h.ha.set("sensor.living_room_feel", "20.9", {"stale": "false"})
        h.tick(minutes=7)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["setpoint_now"], a["call"]), ("settle", 21.5, "false"))
        h.ha.set("sensor.living_room_feel", "22.0", {"stale": "false"})
        h.tick(minutes=5)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["setpoint_now"]), ("hold", 21.5))
        d = [r for r in h.log_lines("decision") if r["zone"] == "family"][-1]
        self.assertEqual((d["gate"], d["drive"]), ("unchanged", 5.0))

    def test_a_long_opening_settles_like_a_short_one_and_ends_with_the_recovery(self):
        h = self.h
        self.open_door()
        h.tick(minutes=3)
        h.tick(minutes=30)
        h.ha.set("binary_sensor.rooftop_door_1_contact", "off")
        h.tick(minutes=2)
        self.assertEqual(h.attrs("family")["mode"], "settle")
        h.tick(minutes=1)
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_an_opening_in_another_zone_leaves_the_family_alone(self):
        h = self.h
        h.ha.set("binary_sensor.kristines_room_window_contact", "on")
        h.tick(minutes=1)
        h.tick(minutes=2)
        self.assertEqual(h.attrs("kristines_room")["mode"], "window")
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_the_opening_state_is_kept_across_a_restart(self):
        h = self.h
        self.open_door()
        h.tick(minutes=1)
        h.tick(minutes=2)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        h2.ha.entities = h.ha.entities
        h2.tick(minutes=1)
        self.assertEqual(h2.attrs("family")["mode"], "window")


class PartyEndToEnd(unittest.TestCase):
    def test_helpers_left_at_their_creation_value_do_nothing(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")
        self.assertEqual(h.attrs("family")["party_window"], ["<none>"])
        h.run_tasks()
        h.notifier.notify.assert_not_awaited()

    def test_a_unreadable_party_helper_is_ignored(self):
        h = Harness(self)
        h.ha.set("input_datetime.heating_party_start", "unavailable")
        h.ha.set("input_datetime.heating_party_end", "garbage")
        h.tick()
        self.assertEqual(h.attrs("family")["party_window"], ["<none>"])

    def test_a_planned_party_pauses_the_family_zone_and_pushes_once(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.ha.set("input_datetime.heating_party_start", "2026-10-17 19:00:00")
        h.ha.set("input_datetime.heating_party_end", "2026-10-17 23:00:00")
        h.tick()  # 12:00: the pause began at 11:00
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["setpoint_now"], a["call"]), ("party", 21.5, "false"))
        self.assertEqual(
            a["party_window"], ["2026-10-17T11:00:00+02:00", "2026-10-17T23:00:00+02:00"]
        )
        h.run_tasks()
        h.notifier.notify.assert_awaited_once()
        kw = h.notifier.notify.await_args.kwargs
        self.assertEqual(kw["target"], "mikkel")
        self.assertIn("would pause until 23:00", kw["message"])
        self.assertIn("This zone is in shadow mode", kw["message"])
        h.tick(minutes=5)
        h.run_tasks()
        self.assertEqual(h.notifier.notify.await_count, 1)
        self.assertEqual(h.attrs("kristines_room")["mode"], "hold")

    def test_no_pause_notice_outside_the_season(self):
        h = Harness(self)
        h.ha.set("input_datetime.heating_party_start", "2026-10-17 19:00:00")
        h.ha.set("input_datetime.heating_party_end", "2026-10-17 23:00:00")
        h.tick()
        h.run_tasks()
        h.notifier.notify.assert_not_awaited()
        self.assertEqual(h.attrs("family")["plan_mode"], "party")

    def test_airing_suggestions_come_during_the_party_and_are_throttled(self):
        h = Harness(self)
        h.ha.set("input_datetime.heating_party_start", "2026-10-17 11:00:00")
        h.ha.set("input_datetime.heating_party_end", "2026-10-17 23:00:00")
        for feel in FEELS["family"]:
            h.ha.set(feel, "23.6", {"stale": "false"})
        h.tick()
        h.run_tasks()
        calls = [c.kwargs["message"] for c in h.notifier.notify.await_args_list]
        self.assertEqual(calls, ["Living area 23.6 °C: open the terrace doors for 10 min"])
        h.tick(minutes=30)
        h.run_tasks()
        self.assertEqual(h.notifier.notify.await_count, 1)
        h.tick(minutes=20)
        h.run_tasks()
        self.assertEqual(h.notifier.notify.await_count, 2)

    def test_stuffy_air_alone_triggers_the_suggestion(self):
        h = Harness(self)
        h.ha.set("input_boolean.party", "on")
        h.ha.set("sensor.kitchen_feel", "22.2", {"air_band": "stuffy"})
        h.tick()
        h.run_tasks()
        msg = h.notifier.notify.await_args.kwargs["message"]
        self.assertEqual(msg, "Living area air stuffy: open the terrace doors for 10 min")

    def test_a_quiet_party_gets_no_suggestion(self):
        h = Harness(self)
        h.ha.set("input_boolean.party", "on")
        h.tick()
        h.run_tasks()
        h.notifier.notify.assert_not_awaited()

    def test_the_manual_switch_pauses_the_zone_for_at_most_twelve_hours(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.ha.set("input_boolean.party", "on")
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "party")
        h.at(23, 59)
        h.ha.set("input_boolean.party", "on", changed=NOW)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "party")  # 11 h 59 min in
        h.at(0, 30, day=18)
        h.ha.set("input_boolean.party", "on", changed=NOW)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_a_failing_push_is_only_a_warning(self):
        h = Harness(self)
        h.notifier.notify = AsyncMock(side_effect=RuntimeError("no phone"))
        h.ha.set("input_boolean.party", "on")
        h.ha.set("sensor.kitchen_feel", "22.2", {"air_band": "poor"})
        h.tick()
        h.run_tasks()
        self.assertTrue(any("push failed" in m for m in h.warnings()))


class BedroomEndToEnd(unittest.TestCase):
    def guarded(self):
        zones = shipped_args(Path("/tmp"))["zones"]
        zones["bathroom"]["bedroom_guard"] = {"enabled": True, "cap": "lower_limit"}
        return Harness(self, zones=zones)

    def evening(self, h, air, window_open="false", day=17):
        h.at(21, 0, day)
        for _ in range(31):
            h.ha.set(
                "sensor.bedroom_feel", str(air), {"stale": "false", "window_open": window_open}
            )
            h.tick()
            h.clock.advance(minutes=5)
        # the loop closes the evening at the first reading after 23:30

    def test_a_warm_bedtime_holds_the_bathroom_at_its_lower_limit_next_morning(self):
        h = self.guarded()
        h.ha.set("input_boolean.heating_season", "on")
        self.evening(h, 21.4)
        self.assertTrue(h.app._loop.hold)
        self.assertEqual(h.log_lines("bedroom")[-1]["action"], "hold")
        h.at(3, 0, day=18)
        h.ha.set("sensor.bedroom_feel", "21.0", {"stale": "false", "window_open": "false"})
        h.tick()
        a = h.attrs("bathroom")
        self.assertEqual((a["mode"], a["setpoint_now"]), ("guard", 19.0))
        self.assertEqual(a["bedroom_hold_back"], "true")
        self.assertEqual(a["bedroom_evening_mean"], "21.4")
        self.assertEqual(h.attrs("family")["mode"], "hold")  # the family zone is never held back

    def test_a_cool_bedtime_gives_the_bathroom_its_morning_heat(self):
        h = self.guarded()
        h.ha.set("input_boolean.heating_season", "on")
        h.app._loop.hold = True
        self.evening(h, 20.2)
        self.assertFalse(h.app._loop.hold)
        h.at(3, 0, day=18)
        h.ha.set("sensor.bedroom_feel", "20.2", {"stale": "false", "window_open": "false"})
        h.tick()
        a = h.attrs("bathroom")
        self.assertEqual((a["mode"], a["setpoint_now"]), ("hold", 22.0))

    def test_airing_before_bed_changes_nothing(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.evening(h, 19.0, window_open="true")
        self.assertFalse(h.app._loop.hold)
        self.assertEqual(h.log_lines("bedroom")[-1]["action"], "keep_airing")

    def test_the_hold_back_survives_a_restart(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.evening(h, 21.4)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        self.assertTrue(h2.app._loop.hold)


class UserAwayEndToEnd(LiveZones, unittest.TestCase):
    """The bathroom's morning heat is for its user: the period runs while person.mikkel or the
    phone's GPS reads home, or while neither can be read."""

    @staticmethod
    def whereabouts(h, person, phone):
        """Where the person entity and the phone read; None takes the entity away."""
        if person is None:
            h.ha.remove("person.mikkel")
        else:
            h.ha.set("person.mikkel", person, {"user_id": MIKKEL_ID})
        if phone is None:
            h.ha.remove(MIKKEL_PHONE)
        else:
            h.ha.set(MIKKEL_PHONE, phone)

    def morning(self, person="home", phone="home"):
        """04:00, inside the bathroom's period, season on."""
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(4, 0, day=18)
        self.whereabouts(h, person, phone)
        h.tick()
        return h

    def live_morning(self, person, phone):
        """04:00 with the bathroom live and its air at 21.5, between the 21.0 it holds while
        Mikkel is out and the period's 22.0."""
        h = self.live("bathroom")
        h.at(4, 0, day=18)
        self.whereabouts(h, person, phone)
        self.air(h, 21.5, "bathroom")
        h.tick()
        if h.calls:
            h.echo()
        return h

    @staticmethod
    def bathroom(h):
        a = h.attrs("bathroom")
        return a["mode"], a["setpoint_now"]

    @staticmethod
    def timeline(h):
        return [
            (b["start"], b["end"], b["mode"], b["setpoint"])
            for b in h.attrs("bathroom")["day_blocks"]
        ]

    def deliver(self, h, entity, old):
        """The state callback for `entity`, with nothing scheduled before it."""
        h.app._eval_pending = False
        h.app.run_in.reset_mock()
        h.app._on_state(entity, "all", old, copy.deepcopy(h.ha.entities[entity]), {})

    def test_at_home_the_period_applies(self):
        h = self.morning()
        self.assertEqual(self.bathroom(h), ("hold", 22.0))
        self.assertEqual(h.zone("bathroom")["state"], 22.0)

    def test_with_both_away_nearby_the_bathroom_holds_21_and_says_why(self):
        for person, phone in (
            ("not_home", "not_home"),
            ("not_home", "Pernille"),
            ("Office", "Pernille"),
        ):
            with self.subTest(person=person, phone=phone):
                h = self.morning(person, phone)
                a = h.attrs("bathroom")
                self.assertEqual((a["mode"], a["plan_mode"]), ("away", "away"))
                self.assertEqual(a["setpoint_now"], 21.0)
                self.assertEqual(h.zone("bathroom")["state"], 21.0)
                self.assertEqual(a["lower_limit"], 19.0)
                self.assertEqual(a["reasons"], ["Mikkel away: holding 21.0 °C"])
                self.assertEqual(self.decision(h, "bathroom")["mode"], "away")
                assert_publishable(self, a)

    def test_one_entity_at_home_keeps_the_period(self):
        for person, phone in (
            ("home", "not_home"),
            ("not_home", "home"),
            ("home", "Pernille"),
            ("Office", "home"),
        ):
            with self.subTest(person=person, phone=phone):
                self.assertEqual(self.bathroom(self.morning(person, phone)), ("hold", 22.0))

    def test_an_entity_that_cannot_be_read_counts_as_home(self):
        for person, phone in (
            ("not_home", "unavailable"),
            ("not_home", "unknown"),
            ("unavailable", "not_home"),
            ("unknown", "unknown"),
            ("not_home", None),
            (None, "not_home"),
            (None, None),
        ):
            with self.subTest(person=person, phone=phone):
                self.assertEqual(self.bathroom(self.morning(person, phone)), ("hold", 22.0))

    def test_no_other_zone_looks_at_where_the_user_is(self):
        h = self.morning("not_home", "not_home")
        for zone in ZONES:
            if zone != "bathroom":
                self.assertEqual(h.attrs(zone)["mode"], "hold", zone)

    def test_coming_home_mid_period_applies_it_from_then(self):
        h = self.morning("not_home", "not_home")
        self.assertEqual(self.bathroom(h), ("away", 21.0))
        h.clock.advance(minutes=5)
        self.whereabouts(h, "home", "not_home")
        self.deliver(h, "person.mikkel", None)
        h.app.run_in.assert_called_once_with(h.app._evaluate_pending, 2)
        h.app._evaluate_pending({})
        self.assertEqual(self.bathroom(h), ("hold", 22.0))
        self.assertEqual(
            self.timeline(h),
            [
                ("04:00", "04:05", "away", "21.0"),
                ("04:05", "09:00", "hold", "22.0"),
                ("09:00", "18:00", "hold", "20.0"),
                ("18:00", "24:00", "hold", "21.5"),
            ],
        )
        self.assertTrue(any("bathroom: away -> hold, aim 22.0" in m for _l, m in h.logs))

    def test_leaving_mid_period_takes_the_heat_away_once_both_entities_have_left(self):
        h = self.morning()
        h.clock.set(6, 30, day=18)
        self.whereabouts(h, "not_home", "home")
        h.tick()
        self.assertEqual(self.bathroom(h), ("hold", 22.0))
        self.whereabouts(h, "not_home", "Pernille")
        h.tick(minutes=5)
        self.assertEqual(self.bathroom(h), ("away", 21.0))
        self.assertEqual(
            self.timeline(h),
            [("04:00", "06:35", "hold", "22.0"), ("06:35", "24:00", "away", "21.0")],
        )
        self.assertTrue(any("bathroom: hold -> away, aim 21.0" in m for _l, m in h.logs))

    FAR = (56.1629, 10.2039)  # about 160 km from HOME_LAT, HOME_LON
    NEAR = (55.70, 12.30)  # about 17 km

    @staticmethod
    def fix(h, point, hours_ago=0):
        h.ha.set(
            MIKKEL_PHONE, h.ha.entities[MIKKEL_PHONE]["state"],
            {"latitude": point[0], "longitude": point[1], "gps_accuracy": 20},
            updated=h.clock.now - timedelta(hours=hours_ago),
        )

    def test_a_phone_far_from_home_holds_the_travel_limit_and_never_tells_where(self):
        h = self.morning("not_home", "not_home")
        self.fix(h, self.FAR)
        h.tick(minutes=5)
        a = h.attrs("bathroom")
        self.assertEqual((a["mode"], a["plan_mode"], a["setpoint_now"]), ("travel", "travel", 17.0))
        self.assertEqual(a["lower_limit"], 17.0)
        self.assertEqual(a["reasons"], ["Mikkel travelling: holding 17.0 °C"])
        self.assertEqual(self.decision(h, "bathroom")["mode"], "travel")
        self.assertTrue(any("bathroom: away -> travel, aim 17.0" in m for _l, m in h.logs))
        assert_publishable(self, a)
        blob = json.dumps(h.published, default=str) + json.dumps(h.logs)
        blob += "".join(f.read_text() for f in (h.tmp / "heating_log").glob("*"))
        for needle in ("56.16", "10.20", "latitude", "longitude"):
            self.assertNotIn(needle, blob)

    def test_away_within_50_km_or_without_a_usable_fix_holds_21(self):
        for point, hours_ago in ((self.NEAR, 0), (self.FAR, 13), ((None, None), 0)):
            with self.subTest(point=point, hours_ago=hours_ago):
                h = self.morning("not_home", "not_home")
                self.fix(h, point, hours_ago)
                h.tick(minutes=5)
                self.assertEqual(self.bathroom(h), ("away", 21.0))
                self.assertEqual(h.attrs("bathroom")["lower_limit"], 19.0)

    def test_a_far_phone_while_mikkel_is_home_changes_nothing(self):
        h = self.morning("home", "not_home")
        self.fix(h, self.FAR)
        h.tick(minutes=5)
        self.assertEqual(self.bathroom(h), ("hold", 22.0))

    def test_coming_home_from_a_trip_restores_the_morning_the_20_day_and_the_evening(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(4, 0, day=18)
        self.whereabouts(h, "not_home", "not_home")
        self.fix(h, self.FAR)
        h.tick()
        self.assertEqual(self.bathroom(h), ("travel", 17.0))
        h.clock.advance(minutes=5)
        self.whereabouts(h, "home", "not_home")
        self.deliver(h, "person.mikkel", None)
        h.app._evaluate_pending({})
        self.assertEqual(self.bathroom(h), ("hold", 22.0))
        self.assertEqual(h.attrs("bathroom")["lower_limit"], 19.0)
        self.assertEqual(
            self.timeline(h),
            [
                ("04:00", "04:05", "travel", "17.0"),
                ("04:05", "09:00", "hold", "22.0"),
                ("09:00", "18:00", "hold", "20.0"),
                ("18:00", "24:00", "hold", "21.5"),
            ],
        )

    def test_a_change_of_state_asks_for_an_evaluation_at_once_and_a_new_position_does_not(self):
        h = self.morning()
        for entity in ("person.mikkel", MIKKEL_PHONE):
            with self.subTest(entity=entity):
                old = copy.deepcopy(h.ha.entities[entity])
                h.ha.update_attrs(entity, latitude=55.1, longitude=12.1)
                self.deliver(h, entity, old)
                h.app.run_in.assert_not_called()
                old = copy.deepcopy(h.ha.entities[entity])
                h.ha.set(entity, "not_home", old["attributes"])
                self.deliver(h, entity, old)
                h.app.run_in.assert_called_once_with(h.app._evaluate_pending, 2)
                self.deliver(h, entity, None)
                h.app.run_in.assert_called_once_with(h.app._evaluate_pending, 2)

    def test_a_new_attribute_of_any_other_trigger_still_asks_for_an_evaluation(self):
        h = self.morning()
        door = "binary_sensor.rooftop_door_1_contact"
        old = copy.deepcopy(h.ha.entities[door])
        h.ha.update_attrs(door, battery=80)
        self.deliver(h, door, old)
        h.app.run_in.assert_called_once_with(h.app._evaluate_pending, 2)

    def test_no_call_starts_while_the_user_is_away_and_one_starts_when_he_comes_home(self):
        h = self.live_morning("not_home", "not_home")
        a = h.attrs("bathroom")
        self.assertEqual((a["mode"], a["call"]), ("away", "false"))
        self.assertEqual(self.sent(h, "bathroom"), [5.0])
        self.whereabouts(h, "home", "not_home")
        h.tick(minutes=5)
        a = h.attrs("bathroom")
        self.assertEqual(
            (a["mode"], a["call"], a["call_since"]), ("hold", "true", self.iso(4, 5, 18))
        )
        self.assertEqual(self.sent(h, "bathroom"), [5.0, 24.0])

    def test_a_call_ends_after_its_minimum_time_when_the_user_leaves(self):
        h = self.live_morning("home", "home")
        a = h.attrs("bathroom")
        self.assertEqual((a["mode"], a["call"]), ("hold", "true"))
        self.assertEqual(self.sent(h, "bathroom"), [24.0])
        self.whereabouts(h, "not_home", "not_home")
        h.tick(minutes=19)
        a = h.attrs("bathroom")
        self.assertEqual((a["mode"], a["call"]), ("away", "true"))
        self.assertEqual(h.app._zones["bathroom"].call_reason, "min_on")
        h.tick(minutes=1)
        a = h.attrs("bathroom")
        self.assertEqual(
            (a["mode"], a["call"], a["call_since"]), ("away", "false", self.iso(4, 20, 18))
        )
        self.assertEqual(self.sent(h, "bathroom"), [24.0, 5.0])


class TerminateAndEvents(unittest.TestCase):
    def test_terminate_saves_the_state_so_a_redeploy_keeps_the_day(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        h.app._zones["kristines_room"].nudge = 0.5
        (h.tmp / "state.json").unlink()
        h.app.terminate()
        self.assertEqual(
            json.loads((h.tmp / "state.json").read_text())["zones"]["kristines_room"]["nudge"], 0.5
        )

    def test_terminate_after_a_failed_start_does_nothing(self):
        args = shipped_args(Path("/tmp"))
        args["zones"] = {}
        h = Harness(self, args=args, forecast=False)
        h.app.terminate()

    def test_a_bedroom_window_opening_between_ticks_still_counts_as_airing(self):
        h = Harness(self)
        h.at(21, 0, day=18)
        h.ha.set("sensor.bedroom_feel", "21.6", {"stale": "false", "window_open": "false"})
        h.tick()
        h.clock.advance(minutes=1)
        opened = {"stale": "false", "window_open": "true"}
        h.ha.set("sensor.bedroom_feel", "21.6", opened)
        new = copy.deepcopy(h.ha.entities["sensor.bedroom_feel"])
        h.app._on_state("sensor.bedroom_feel", "all", None, new, {})
        self.assertTrue(h.app._loop.airing)

    def test_a_day_with_the_clocks_going_back_has_a_timeline_to_midnight(self):
        h = Harness(self, zones=zones_with_family_night_offset())
        h.at(12, 0, day=25)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        blocks = h.attrs("family")["day_blocks"]
        self.assertEqual(blocks[-1]["end"], "24:00")
        self.assertEqual([b["setpoint"] for b in blocks], ["21.5", "21.0"])
        self.assertEqual(h.attrs("family")["next_change_at"], "2026-10-25T19:00:00+01:00")

    def test_a_day_without_ticks_is_not_reported(self):
        h = Harness(self)
        h.tick()
        h.app._zones["kristines_room"].days["2026-10-15"] = hp.ZoneDay("2026-10-15")
        h.at(4, 0, day=18)
        h.app._daily_rollup({})
        days = {r["day"] for r in h.log_lines("daily") if r["zone"] == "kristines_room"}
        self.assertEqual(days, {"2026-10-17"})

    def test_a_normal_day_logs_no_errors(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(0, 0)
        for _ in range(24 * 12):
            h.tick()
            h.clock.advance(minutes=5)
        self.assertEqual([m for lvl, m in h.logs if lvl == "ERROR"], [])


class Timeline(unittest.TestCase):
    def test_finished_blocks_stay_on_the_day_timeline(self):
        h = Harness(self, zones=zones_with_family_night_offset())
        h.ha.set("input_boolean.heating_season", "on")
        h.at(5, 50)
        h.tick()
        h.at(6, 5)
        h.tick()
        blocks = h.attrs("family")["day_blocks"]
        self.assertEqual(
            [(b["start"], b["end"], b["setpoint"]) for b in blocks],
            [
                ("05:50", "06:05", "21.0"),
                ("06:05", "19:00", "21.5"),
                ("19:00", "24:00", "21.0"),
            ],
        )


class DecisionLogging(unittest.TestCase):
    def test_the_first_tick_logs_every_zone(self):
        h = Harness(self)
        h.tick()
        decisions = h.log_lines("decision")
        self.assertEqual(sorted(d["zone"] for d in decisions), sorted(ZONES))
        fam = next(d for d in decisions if d["zone"] == "family")
        self.assertEqual(fam["ts"], "2026-10-17T12:00:00+02:00")
        self.assertEqual(
            (fam["mode"], fam["plan_mode"], fam["aim"]), ("off_season", "hold", 21.5)
        )
        self.assertEqual(
            (fam["allowed"], fam["call"], fam["call_reason"], fam["drive"]),
            (False, False, "not_allowed", 5.0),
        )
        self.assertEqual(fam["air_source"], "feel")
        self.assertEqual(fam["limits"], [19.0, 24.0])
        for gone in ("trim", "setpoint", "bias_sample"):
            self.assertNotIn(gone, fam)
        self.assertIn("budget", fam)
        self.assertEqual(fam["budget"][0]["day"], "2026-10-17")
        self.assertEqual(fam["reasons"][0], "Heating season is off: the thermostat is left alone")

    def test_an_unchanged_tick_writes_nothing_until_half_an_hour_has_passed(self):
        h = Harness(self)
        h.tick()
        n = len(h.log_lines("decision"))
        h.tick(minutes=5)
        h.tick(minutes=5)
        self.assertEqual(len(h.log_lines("decision")), n)
        h.tick(minutes=20)
        self.assertEqual(len(h.log_lines("decision")), 2 * n)

    def test_a_change_is_logged_at_once(self):
        h = Harness(self)
        h.tick()
        n = len(h.log_lines("decision"))
        h.ha.set("input_boolean.heating_season", "on")
        h.tick(minutes=5)
        self.assertEqual(len(h.log_lines("decision")), 2 * n)

    def test_one_file_per_month_with_one_json_object_per_line(self):
        h = Harness(self)
        h.tick()
        files = list((h.tmp / "heating_log").glob("*"))
        self.assertEqual([f.name for f in files], ["decisions-2026-10.jsonl"])
        for line in files[0].read_text().splitlines():
            json.loads(line)

    def test_the_log_directory_is_created(self):
        h = Harness(self)
        self.assertTrue((h.tmp / "heating_log").is_dir())

    def test_an_unwritable_log_dir_is_a_single_warning(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "blocker").write_text("x")
        args = shipped_args(tmp)
        args["log_dir"] = str(tmp / "blocker" / "sub")
        h = Harness(self, args=args)
        h.tick()
        h.tick(minutes=40)
        self.assertEqual(len([m for m in h.warnings() if "decision log not written" in m]), 1)
        self.assertEqual(h.attrs("family")["setpoint_now"], 21.5)


class StateFile(unittest.TestCase):
    def test_state_round_trips_through_a_restart(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        h.tick(minutes=10)  # the drive has been stable long enough to be "written"
        h.app._zones["kristines_room"].nudge = 0.5
        h.tick(minutes=5)
        saved = json.loads((h.tmp / "state.json").read_text())
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["zones"]["kristines_room"]["nudge"], 0.5)
        self.assertEqual(saved["zones"]["family"]["last_planned"], 5.0)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        self.assertEqual(h2.app._zones["kristines_room"].nudge, 0.5)
        self.assertEqual(h2.app._zones["family"].last_planned, 5.0)
        self.assertIn("2026-10-17", h2.app._zones["family"].days)
        self.assertEqual(h2.app._zones["family"].blocks.day, "2026-10-17")

    def test_a_missing_file_gives_the_yaml_defaults(self):
        h = Harness(self, forecast=False)
        for rt in h.app._zones.values():
            self.assertEqual((rt.nudge, rt.call.on, rt.last_planned), (0.0, False, None), rt.zc.name)
            self.assertEqual(rt.tracker.state, hp.STATE_CLOSED)
        self.assertEqual(h.warnings(), [])

    def test_a_corrupt_file_gives_the_yaml_defaults_and_a_warning(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        Path(args["state_file"]).write_text('{"zones": {"family": {"call": ')
        h = Harness(self, args=args, forecast=False)
        self.assertFalse(h.app._zones["family"].call.on)
        self.assertTrue(any("state file unreadable" in m for m in h.warnings()))

    def test_valid_json_of_the_wrong_shape_is_survivable(self):
        for junk in (
            "[]",
            '"x"',
            '{"zones": [], "bedroom": 3, "house": 4, "notified": 5}',
            '{"zones": {"family": {"call": "abc", "window": [], "days": {"2026-10-17": 5}, "bucket": 1}}}',
            '{"zones": {"family": {"call": {"on": "yes", "since": "x", "high": []}, "blocks": 7}}}',
        ):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            args = shipped_args(tmp)
            Path(args["state_file"]).write_text(junk)
            h = Harness(self, args=args, forecast=False)
            h.tick()
            self.assertEqual(h.attrs("family")["setpoint_now"], 21.5, junk)
            self.assertFalse(h.app._zones["family"].call.on, junk)

    def test_the_keys_of_the_old_model_load_and_are_dropped_at_the_next_save(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        old_day = {"ticks": 3, "trim_samples": [[1.0, 0.5]], "bias_hours": 2.0}
        Path(args["state_file"]).write_text(
            json.dumps(
                {
                    "version": 1,
                    "zones": {
                        "family": {
                            "trim": 9.0,
                            "warm": True,
                            "comfort": {
                                "samples": {"sensor.living_room_feel": [[1, 20.0]]},
                                "active": "sensor.living_room_feel",
                            },
                            "nudge": 4.0,
                            "last_planned": 22.0,
                            "days": {"2026-10-17": old_day},
                            "window": {"state": "settle", "long": True, "settle_since": 1.0},
                        }
                    },
                }
            )
        )
        h = Harness(self, args=args, forecast=False)
        rt = h.app._zones["family"]
        self.assertEqual((rt.nudge, rt.last_planned), (0.0, 22.0))  # no residents, no nudge
        self.assertFalse(rt.call.on)
        h.tick()
        saved = json.loads(Path(args["state_file"]).read_text())["zones"]["family"]
        for gone in ("trim", "warm", "comfort"):
            self.assertNotIn(gone, saved)
        self.assertEqual(h.warnings(), [])

    def test_a_resident_nudge_is_kept_within_its_bounds(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        Path(args["state_file"]).write_text(
            json.dumps(
                {"zones": {"kristines_room": {"nudge": 0.5}, "claudias_room": {"nudge": -3.0}}}
            )
        )
        h = Harness(self, args=args, forecast=False)
        h.tick()
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 21.0)
        self.assertEqual(h.attrs("kristines_room")["nudge"], "+0.5")
        self.assertEqual(h.attrs("claudias_room")["setpoint_now"], 20.5)

    def test_the_file_is_replaced_atomically(self):
        h = Harness(self)
        h.tick()
        self.assertFalse((h.tmp / "state.json.tmp").exists())

    def test_an_unwritable_state_file_is_a_single_warning(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "blocker").write_text("x")
        args = shipped_args(tmp)
        args["state_file"] = str(tmp / "blocker" / "state.json")
        h = Harness(self, args=args)
        h.tick()
        h.tick(minutes=5)
        self.assertEqual(len([m for m in h.warnings() if "state not saved" in m]), 1)
        self.assertEqual(h.attrs("family")["setpoint_now"], 21.5)


class DailyRollup(unittest.TestCase):
    def run_day(self, h, bias=0.6):
        """A whole day of five-minute ticks with fresh readings and a thermostat that reads
        `bias` above the room."""
        h.at(0, 0)
        for _ in range(24 * 12):
            h.ha.set(
                THERMOSTATS["family"],
                "heat",
                {
                    "current_temperature": round(22.2 + bias, 1),
                    "temperature": 15.0,
                    "hvac_action": "heating" if h.clock.now.hour < 6 else "idle",
                },
            )
            h.tick()
            h.clock.advance(minutes=5)

    def test_the_day_closes_at_03_00_with_its_statistics(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.run_day(h)
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        daily = [r for r in h.log_lines("daily") if r["zone"] == "family"]
        self.assertEqual(len(daily), 1)
        rec = daily[0]
        self.assertEqual(rec["day"], "2026-10-17")
        self.assertEqual(rec["call_hours"], 6.0)
        self.assertEqual(rec["in_band_pct"], 0.0)  # no target of the day is within 0.5 of 22.2
        self.assertEqual(rec["ticks"], 24 * 12)
        for gone in ("trim_before", "trim_after", "trim_bias", "trim_valid_hours"):
            self.assertNotIn(gone, rec)
        house = [r for r in h.log_lines("daily") if r["zone"] == "_house"]
        self.assertEqual(len(house), 1)
        self.assertEqual(house[0]["ticks"], 24 * 12)

    def test_the_closed_day_leaves_the_zones_settings_alone(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.run_day(h)
        before = {zone: (rt.nudge, rt.call.snapshot()) for zone, rt in h.app._zones.items()}
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        after = {zone: (rt.nudge, rt.call.snapshot()) for zone, rt in h.app._zones.items()}
        self.assertEqual(after, before)

    def test_off_season_ticks_are_not_judged_against_the_band(self):
        h = Harness(self)
        self.run_day(h)
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        rec = next(r for r in h.log_lines("daily") if r["zone"] == "family")
        self.assertEqual((rec["band_ticks"], rec["in_band_pct"]), (0, None))

    def test_yesterday_shows_up_on_the_sensor_and_the_day_is_not_closed_twice(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.run_day(h)
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        h.tick(minutes=5)
        y = h.attrs("family")["yesterday"]
        self.assertEqual((y["day"], y["call_hours"]), ("2026-10-17", 6.0))
        self.assertNotIn("trim", y)
        h.app._daily_rollup({})
        self.assertEqual(len([r for r in h.log_lines("daily") if r["zone"] == "family"]), 1)

    def test_a_missed_03_00_is_made_up_at_the_next_tick(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.run_day(h)
        h.at(7, 0, day=18)
        h.tick()
        self.assertEqual(len([r for r in h.log_lines("daily") if r["zone"] == "family"]), 1)

    def test_before_03_00_the_finished_day_waits(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.run_day(h)
        h.at(1, 0, day=18)
        h.tick()
        self.assertEqual([r for r in h.log_lines("daily") if r["zone"] == "family"], [])


class TriggersAndCache(unittest.TestCase):
    def test_an_opening_schedules_one_evaluation_and_a_temperature_does_not(self):
        h = Harness(self, forecast=False)
        new = {
            "state": "on",
            "attributes": {},
            "last_updated": h.clock.now.isoformat(),
            "last_changed": h.clock.now.isoformat(),
        }
        h.app._on_state("binary_sensor.rooftop_door_1_contact", "all", None, new, {})
        h.app._on_state("binary_sensor.rooftop_door_1_contact", "all", None, new, {})
        h.app.run_in.assert_called_once_with(h.app._evaluate_pending, 2)
        h.app.run_in.reset_mock()
        h.app._eval_pending = False
        h.app._on_state(
            THERMOSTATS["family"],
            "all",
            None,
            dict(new, state="heat", attributes={"current_temperature": 22.0}),
            {},
        )
        h.app.run_in.assert_not_called()

    def test_a_callback_value_newer_than_the_store_wins_and_an_older_one_loses(self):
        h = Harness(self, forecast=False)
        entity = "binary_sensor.rooftop_door_1_contact"
        h.ha.set(entity, "off", updated=h.clock.now - timedelta(minutes=10))
        newer = (h.clock.now - timedelta(minutes=1)).isoformat()
        h.app._on_state(
            entity,
            "all",
            None,
            {"state": "on", "attributes": {}, "last_updated": newer, "last_changed": newer},
            {},
        )
        self.assertEqual(h.app._state_of(entity), "on")
        h.ha.set(entity, "off", updated=h.clock.now)
        self.assertEqual(h.app._state_of(entity), "off")

    def test_the_evaluation_that_follows_runs_as_an_event_not_a_tick(self):
        h = Harness(self, forecast=False)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        before = h.app._house[h.clock.now.date().isoformat()]["ticks"]
        h.app._evaluate_pending({})
        self.assertEqual(h.app._house[h.clock.now.date().isoformat()]["ticks"], before)

    def test_a_thermostat_report_updates_the_setpoint_a_change_is_measured_from(self):
        h = Harness(self, forecast=False)
        h.tick()
        rt = h.app._zones["family"]
        self.assertEqual(rt.setpoint_seen, 15.0)
        h.clock.advance(hours=3)
        h.app._on_state(
            THERMOSTATS["family"],
            "all",
            None,
            {
                "state": "heat",
                "attributes": {"temperature": 15.0},
                "last_updated": h.clock.now.isoformat(),
            },
            {},
        )
        self.assertEqual(rt.setpoint_seen, 15.0)
        h.app._on_state(
            THERMOSTATS["family"],
            "all",
            None,
            {
                "state": "heat",
                "attributes": {"temperature": 22.0},
                "last_updated": h.clock.now.isoformat(),
            },
            {},
        )
        self.assertEqual(rt.setpoint_seen, 22.0)


class PluginStarted(unittest.TestCase):
    def test_everything_is_republished_after_a_home_assistant_restart(self):
        h = Harness(self)
        h.tick()
        first = len(h.published)
        h.tick(minutes=5)
        self.assertEqual(
            len(h.published) - first, 1
        )  # only the controller sensor (it carries the tick time)
        before = len(h.published)
        h.app._on_plugin_started("plugin_started", {}, {})
        entities = [p["entity"] for p in h.published[before:]]
        self.assertEqual(
            sorted(entities),
            sorted([f"sensor.heating_zone_{z}" for z in ZONES] + ["sensor.heating_controller"]),
        )


class Publishing(unittest.TestCase):
    def test_nothing_unchanged_is_published_twice(self):
        h = Harness(self)
        h.tick()
        zone_publishes = len(
            [p for p in h.published if p["entity"].startswith("sensor.heating_zone_")]
        )
        h.tick(minutes=5)
        self.assertEqual(
            len([p for p in h.published if p["entity"].startswith("sensor.heating_zone_")]),
            zone_publishes,
        )

    def test_the_publishable_helper(self):
        got = hc._publishable(
            {
                "a": 0,
                "b": 0.0,
                "c": False,
                "d": True,
                "e": None,
                "f": [],
                "g": [0, 1.5, None],
                "h": {"i": 0},
            }
        )
        self.assertEqual(
            got,
            {
                "a": "0",
                "b": "0.0",
                "c": "false",
                "d": "true",
                "f": ["<none>"],
                "g": ["0", 1.5],
                "h": {"i": "0"},
            },
        )

    def test_a_publish_failure_is_a_warning_not_a_crash(self):
        h = Harness(self)

        def boom(*a, **kw):
            raise RuntimeError("ha down")

        h.app.set_state = boom
        h.tick()
        self.assertTrue(any("publish failed" in m for m in h.warnings()))


LIVING_FLOOR = "sensor.living_room_floor_thermometer_temperature"
DINING_FLOOR = "sensor.dining_room_floor_thermometer_temperature"
KRISTINE_WARMER = BUTTONS[("kristines_room", "warmer")]
KRISTINE_COLDER = BUTTONS[("kristines_room", "colder")]
CLAUDIA_WARMER = BUTTONS[("claudias_room", "warmer")]
CLAUDIA_COLDER = BUTTONS[("claudias_room", "colder")]
KRISTINE_TARGET = "input_number.heating_target_kristines_room"


class NudgeButtons(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self).season()

    def nudge(self, zone="kristines_room"):
        return self.h.app._zones[zone].nudge

    def test_a_press_moves_the_residents_nudge_and_the_plan(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(self.nudge(), 0.5)
        h.app.run_in.assert_called_with(h.app._evaluate_pending, 2)
        h.app._evaluate_pending({})
        a = h.attrs("kristines_room")
        self.assertEqual(
            (a["nudge"], a["setpoint_now"], a["profile_offset"]), ("+0.5", 21.0, "+0.0")
        )
        self.assertEqual(h.zone("kristines_room")["state"], 21.0)
        self.assertEqual(
            a["last_signal"],
            {
                "at": "2026-10-17T12:00:00+02:00",
                "who": "kristine",
                "source": "button",
                "direction": "warmer",
                "effect": "nudge",
            },
        )
        d = [r for r in h.log_lines("decision") if r["zone"] == "kristines_room"][-1]
        self.assertEqual((d["gate"], d["would_write"], d["nudge"]), ("priority:nudge", True, 0.5))
        self.assertEqual(h.attrs("claudias_room")["nudge"], "+0.0")

    def test_the_press_is_logged_and_kept_on_disk_at_once(self):
        h = self.h
        h.press(KRISTINE_COLDER, KRISTINE_ID)
        (rec,) = h.signals("kristines_room")
        self.assertEqual(rec["type"], "signal")
        self.assertEqual(
            {k: rec[k] for k in ("kind", "source", "who", "direction", "effect", "nudge")},
            {
                "kind": "button",
                "source": "button",
                "who": "kristine",
                "direction": -1,
                "effect": "nudge",
                "nudge": [0.0, -0.5],
            },
        )
        saved = json.loads((h.tmp / "state.json").read_text())
        self.assertEqual(saved["zones"]["kristines_room"]["nudge"], -0.5)
        self.assertEqual(saved["zones"]["kristines_room"]["nudge_dir"], -1)
        self.assertEqual(saved["signals"][-1]["effect"], "nudge")

    def test_presses_in_one_direction_within_five_minutes_are_one_step(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.clock.advance(minutes=4)
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(
            h.effects("kristines_room"), ["nudge", "nudge_collapsed", "nudge_collapsed"]
        )
        self.assertEqual(self.nudge(), 0.5)
        h.clock.advance(minutes=2)
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(self.nudge(), 1.0)

    def test_a_press_the_other_way_is_a_new_step(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.press(KRISTINE_COLDER, KRISTINE_ID)
        self.assertEqual(h.effects("kristines_room"), ["nudge", "nudge"])
        self.assertEqual(self.nudge(), 0.0)

    def test_the_nudge_stops_at_its_bounds_in_both_directions(self):
        h = self.h
        for _ in range(4):
            h.press(KRISTINE_WARMER, KRISTINE_ID)
            h.clock.advance(minutes=6)
        self.assertEqual(
            h.effects("kristines_room"), ["nudge", "nudge", "nudge_limit", "nudge_limit"]
        )
        self.assertEqual(self.nudge(), 1.0)
        h.app._evaluate_pending({})
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 21.5)
        for _ in range(6):
            h.press(KRISTINE_COLDER, KRISTINE_ID)
            h.clock.advance(minutes=6)
        self.assertEqual(self.nudge(), -1.0)
        self.assertEqual(h.effects("kristines_room")[-1], "nudge_limit")

    def test_only_the_rooms_resident_and_the_owner_may_press(self):
        cases = [
            (MIKKEL_ID, None, "nudge", "mikkel"),
            (CLAUDIA_ID, None, "ignored", "claudia"),
            (STRANGER_ID, None, "ignored", "unknown"),
            (APP_ID, None, "ignored", "appdaemon"),
            (None, "automation-context", "ignored", "automation"),
            (None, None, "ignored", "unknown"),
        ]
        for user, parent, effect, who in cases:
            h = Harness(self).season()
            h.press(KRISTINE_WARMER, user, parent)
            (rec,) = h.signals("kristines_room")
            self.assertEqual((rec["effect"], rec["who"]), (effect, who), (user, parent))
            self.assertEqual(
                h.app._zones["kristines_room"].nudge, 0.5 if effect == "nudge" else 0.0
            )

    def test_claudias_buttons_move_claudias_nudge_and_hers_only(self):
        h = self.h
        h.press(CLAUDIA_COLDER, CLAUDIA_ID)
        self.assertEqual((self.nudge("claudias_room"), self.nudge()), (-0.5, 0.0))
        h.press(CLAUDIA_WARMER, KRISTINE_ID)
        self.assertEqual(h.effects("claudias_room"), ["nudge", "ignored"])
        self.assertEqual(self.nudge("claudias_room"), -0.5)

    def test_each_room_has_its_own_five_minute_window(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.press(CLAUDIA_WARMER, CLAUDIA_ID)
        self.assertEqual((self.nudge(), self.nudge("claudias_room")), (0.5, 0.5))

    def test_the_first_press_of_a_button_nobody_has_pressed_counts(self):
        h = self.h
        h.ha.set(KRISTINE_WARMER, "unknown")
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(self.nudge(), 0.5)
        self.assertEqual(h.effects("kristines_room"), ["nudge"])

    def test_an_entity_coming_back_after_a_restart_is_not_a_press(self):
        h = self.h
        for old, new in (
            ("unavailable", PRESS_TIME),
            ("unavailable", "unknown"),
            (PRESS_TIME, "unavailable"),
            (PRESS_TIME, "unknown"),
        ):
            h.press(KRISTINE_WARMER, KRISTINE_ID, old=old, new=new)
        h.app._on_button(
            "state_changed", h.event(KRISTINE_WARMER, None, PRESS_TIME, KRISTINE_ID, None), {}
        )
        h.app._on_button(
            "state_changed", h.event(KRISTINE_WARMER, PRESS_TIME, PRESS_TIME, KRISTINE_ID, None), {}
        )
        self.assertEqual(h.signals(), [])
        self.assertEqual(self.nudge(), 0.0)

    def test_junk_events_do_nothing_and_raise_nothing(self):
        h = self.h
        for data in (
            None,
            {},
            {"entity_id": KRISTINE_WARMER},
            {"entity_id": KRISTINE_WARMER, "old_state": "x", "new_state": "y"},
            {
                "entity_id": "input_button.other",
                "old_state": {"state": "a"},
                "new_state": {"state": "b"},
            },
        ):
            h.app._on_button("state_changed", data, {})
        self.assertEqual((h.signals(), self.nudge()), ([], 0.0))
        self.assertEqual([m for lvl, m in h.logs if lvl == "ERROR"], [])

    def test_the_buttons_are_the_four_the_yaml_names(self):
        h = self.h
        self.assertEqual(h.app._buttons.keys(), set(BUTTONS.values()))
        self.assertEqual(h.app._buttons[CLAUDIA_COLDER], ("claudias_room", -1))
        self.assertEqual(h.app._buttons[KRISTINE_WARMER], ("kristines_room", 1))

    def test_a_missing_button_is_one_warning_each_and_the_app_carries_on(self):
        h = Harness(self, with_house=False, forecast=False)
        warned = [m for m in h.warnings() if "not known to AppDaemon" in m]
        self.assertEqual(len(warned), len(BUTTONS))
        self.assertTrue(all(any(b in m for m in warned) for b in BUTTONS.values()))
        listened = [
            c.kwargs["entity_id"]
            for c in h.app.listen_event.call_args_list
            if c.args[0] == h.app._on_button
        ]
        self.assertEqual(sorted(listened), sorted(BUTTONS.values()))
        house(h.ha)
        h.season()
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)
        self.assertEqual(
            len([m for m in h.warnings() if "not known to AppDaemon" in m]), len(BUTTONS)
        )

    def test_a_state_store_that_raises_for_a_new_entity_does_not_stop_the_wiring(self):
        h = Harness(self, forecast=False)

        def boom(*args, **kw):
            raise RuntimeError("not found")

        h.app.get_state = boom
        h.app.listen_event.reset_mock()
        h.app._wire_events()
        self.assertEqual(
            len(h.app.listen_event.call_args_list), 2 + 1 + len(BUTTONS) + len(h.app._owned)
        )


class TargetHelpers(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self).season()

    def test_the_owner_changing_a_residents_target_resets_her_nudge(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.helper(KRISTINE_TARGET, 20.5, 22.0, MIKKEL_ID)
        rt = h.app._zones["kristines_room"]
        self.assertEqual((rt.nudge, rt.nudge_at, rt.nudge_dir), (0.0, None, 0))
        rec = h.signals("kristines_room")[-1]
        self.assertEqual(
            (rec["kind"], rec["source"], rec["who"], rec["direction"], rec["effect"]),
            ("target", "ha", "mikkel", 1, "nudge_reset"),
        )
        self.assertEqual(rec["nudge"], [0.5, 0.0])
        h.app._evaluate_pending({})
        a = h.attrs("kristines_room")
        self.assertEqual((a["base_target"], a["nudge"], a["setpoint_now"]), (22.0, "+0.0", 22.0))
        saved = json.loads((h.tmp / "state.json").read_text())
        self.assertEqual(saved["zones"]["kristines_room"]["nudge"], 0.0)

    def test_a_reset_lets_the_next_press_count_at_once(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.helper(KRISTINE_TARGET, 20.5, 22.0, MIKKEL_ID)
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)

    def test_the_owner_changing_a_target_with_no_nudge_just_sets_it(self):
        h = self.h
        h.helper(KRISTINE_TARGET, 20.5, 22.0, MIKKEL_ID)
        h.helper("input_number.heating_target_family", 21.5, 21.0, MIKKEL_ID)
        self.assertEqual(h.effects(), ["target_set", "target_set"])
        self.assertEqual([r["direction"] for r in h.signals()], [1, -1])
        h.tick(minutes=1)
        self.assertEqual(h.zone("family")["state"], 21.0)

    def test_somebody_else_changing_a_target_is_logged_as_a_revert_that_is_not_performed(self):
        h = self.h
        h.helper("input_number.heating_target_family", 21.5, 24.0, KRISTINE_ID)
        (rec,) = h.signals("family")
        self.assertEqual(
            (rec["effect"], rec["who"], rec["old"], rec["new"], rec["revert_to"]),
            ("would_revert", "kristine", "21.5", "24.0", "21.5"),
        )
        h.tick(minutes=1)
        self.assertEqual(h.zone("family")["state"], 24.0)
        for name in ("call_service", "turn_on", "turn_off", "toggle", "select_option", "set_value"):
            getattr(h.app, name).assert_not_called()

    def test_every_kind_of_stranger_would_be_reverted_but_an_app_or_the_house_would_not(self):
        cases = [
            (CLAUDIA_ID, None, "would_revert"),
            (STRANGER_ID, None, "would_revert"),
            (None, "automation-context", "would_revert"),
            (APP_ID, None, "target_set"),
            (None, None, "target_set"),
        ]
        for user, parent, effect in cases:
            h = Harness(self).season()
            h.helper("input_number.heating_target_family", 21.5, 22.5, user, parent)
            self.assertEqual(h.effects("family"), [effect], (user, parent))

    def test_an_app_changing_a_residents_target_leaves_her_nudge_alone(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.helper(KRISTINE_TARGET, 20.5, 22.0, APP_ID)
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)
        self.assertEqual(h.effects("kristines_room"), ["nudge", "target_set"])

    def test_the_other_owned_helpers_are_only_watched_for_strangers(self):
        h = self.h
        h.helper("input_boolean.heating_season", "on", "off", MIKKEL_ID)
        h.helper("input_boolean.heating_season", "off", "on", APP_ID)
        self.assertEqual(h.signals(), [])
        h.helper("input_boolean.party", "off", "on", KRISTINE_ID)
        h.helper("input_number.heating_bedroom_ceiling", 21.0, 24.0, None, "automation-context")
        recs = h.signals()
        self.assertEqual(
            [(r["zone"], r["kind"], r["effect"]) for r in recs],
            [(None, "helper", "would_revert")] * 2,
        )
        self.assertEqual(
            [r["entity"] for r in recs],
            ["input_boolean.party", "input_number.heating_bedroom_ceiling"],
        )

    def test_a_helper_coming_back_after_a_restart_is_not_a_change(self):
        h = self.h
        h.helper("input_number.heating_target_family", "unavailable", 21.5, KRISTINE_ID)
        h.helper("input_number.heating_target_family", 21.5, "unknown", KRISTINE_ID)
        h.helper("input_number.heating_target_family", None, 21.5, KRISTINE_ID)
        h.helper("input_number.heating_target_family", 21.5, 21.5, KRISTINE_ID)
        self.assertEqual(h.signals(), [])

    def test_junk_events_do_nothing_and_raise_nothing(self):
        h = self.h
        for data in (
            None,
            {},
            {"entity_id": "input_number.heating_target_family"},
            {"old_state": 1, "new_state": 2},
        ):
            h.app._on_helper("state_changed", data, {})
        self.assertEqual(h.signals(), [])
        self.assertEqual([m for lvl, m in h.logs if lvl == "ERROR"], [])


class ThermostatChanges(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self).season()

    def one(self, zone="kristines_room"):
        (rec,) = self.h.signals(zone)
        return rec

    def test_a_keypad_change_in_a_residents_room_is_her_nudge(self):
        h = self.h
        h.report("kristines_room", temperature=15.5)
        rec = self.one()
        self.assertEqual(
            {
                k: rec[k]
                for k in ("kind", "source", "who", "direction", "effect", "old", "new", "nudge")
            },
            {
                "kind": "setpoint",
                "source": "keypad",
                "who": "kristine",
                "direction": 1,
                "effect": "nudge",
                "old": 15.0,
                "new": 15.5,
                "nudge": [0.0, 0.5],
            },
        )
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)
        saved = json.loads((h.tmp / "state.json").read_text())
        self.assertEqual(saved["zones"]["kristines_room"]["nudge"], 0.5)
        h.app._evaluate_pending({})
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 21.0)

    def test_the_size_of_a_keypad_change_does_not_matter_only_its_direction(self):
        h = self.h
        h.report("claudias_room", temperature=5.0)
        rec = self.one("claudias_room")
        self.assertEqual((rec["direction"], rec["who"], rec["effect"]), (-1, "claudia", "nudge"))
        self.assertEqual(h.app._zones["claudias_room"].nudge, -0.5)

    def test_a_residents_own_change_through_home_assistant_is_her_nudge_too(self):
        h = self.h
        h.service_call("kristines_room", "set_temperature", KRISTINE_ID, temperature=14.0)
        h.report("kristines_room", temperature=14.0)
        rec = self.one()
        self.assertEqual(
            (rec["source"], rec["who"], rec["direction"], rec["effect"], rec["call"]),
            ("ha", "kristine", -1, "nudge", "set_temperature"),
        )
        self.assertEqual(h.app._zones["kristines_room"].nudge, -0.5)

    def test_the_owner_moving_a_thermostat_is_a_target_step_that_is_only_recorded(self):
        h = self.h
        h.service_call("kristines_room", "set_temperature", MIKKEL_ID, temperature=16.0)
        h.report("kristines_room", temperature=16.0)
        rec = self.one()
        self.assertEqual(
            (rec["source"], rec["who"], rec["effect"], rec["target_from"], rec["target_to"]),
            ("ha", "mikkel", "would_set_target", 20.5, 21.0),
        )
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.0)
        h.service_call("family", "set_temperature", MIKKEL_ID, temperature=14.0)
        h.report("family", temperature=14.0)
        rec = self.one("family")
        self.assertEqual(
            (rec["effect"], rec["target_from"], rec["target_to"]), ("would_set_target", 21.5, 21.0)
        )
        self.assertNotIn("nudge", rec)
        h.tick(minutes=1)
        self.assertEqual(h.zone("family")["state"], 21.5)
        self.assertEqual(h.zone("kristines_room")["state"], 20.5)

    def test_the_target_step_stays_inside_the_helpers_range(self):
        h = self.h
        h.ha.set(KRISTINE_TARGET, "22.0", {"min": 18, "max": 22})
        h.service_call("kristines_room", "set_temperature", MIKKEL_ID, temperature=16.0)
        h.report("kristines_room", temperature=16.0)
        rec = self.one()
        self.assertEqual((rec["target_from"], rec["target_to"]), (22.0, 22.0))

    def test_an_automation_or_another_app_changing_a_setpoint_would_be_restored(self):
        for user, parent, source, who in (
            (None, "automation-context", "automation", "automation"),
            (APP_ID, None, "app", "appdaemon"),
        ):
            h = Harness(self).season()
            h.service_call("kristines_room", "set_temperature", user, parent, temperature=18.0)
            h.report("kristines_room", temperature=18.0)
            rec = self.one_of(h, "kristines_room")
            self.assertEqual(
                (rec["source"], rec["who"], rec["effect"]), (source, who, "would_restore")
            )
            self.assertEqual(h.app._zones["kristines_room"].nudge, 0.0)

    @staticmethod
    def one_of(h, zone):
        (rec,) = h.signals(zone)
        return rec

    def test_a_keypad_change_on_the_shared_thermostat_would_be_restored(self):
        h = self.h
        h.report("family", temperature=22.0)
        rec = self.one("family")
        self.assertEqual(
            (rec["source"], rec["who"], rec["effect"]), ("keypad", "unknown", "would_restore")
        )

    def test_a_resident_changing_another_zones_thermostat_would_be_restored(self):
        h = self.h
        h.service_call("claudias_room", "set_temperature", KRISTINE_ID, temperature=25.0)
        h.report("claudias_room", temperature=25.0)
        rec = self.one("claudias_room")
        self.assertEqual(
            (rec["source"], rec["who"], rec["effect"]), ("ha", "kristine", "would_restore")
        )
        self.assertEqual(h.app._zones["claudias_room"].nudge, 0.0)

    def test_a_call_older_than_two_minutes_or_for_another_value_is_not_the_cause(self):
        h = self.h
        h.service_call("kristines_room", "set_temperature", MIKKEL_ID, temperature=16.0)
        h.clock.advance(seconds=121)
        h.report("kristines_room", temperature=16.0)
        self.assertEqual(self.one()["source"], "keypad")
        h2 = Harness(self).season()
        h2.service_call("kristines_room", "set_temperature", MIKKEL_ID, temperature=20.0)
        h2.report("kristines_room", temperature=16.0)
        self.assertEqual(self.one_of(h2, "kristines_room")["source"], "keypad")

    def test_a_mode_change_is_classified_by_who_made_it(self):
        h = self.h
        h.service_call("kristines_room", "set_hvac_mode", MIKKEL_ID, hvac_mode="off")
        h.report("kristines_room", state="off")
        h.report("claudias_room", state="off")
        h.service_call("bathroom", "turn_off", KRISTINE_ID)
        h.report("bathroom", state="off")
        recs = {r["zone"]: r for r in h.signals()}
        self.assertEqual(
            {
                z: (r["kind"], r["source"], r["who"], r["effect"], r["old"], r["new"])
                for z, r in recs.items()
            },
            {
                "kristines_room": ("mode", "ha", "mikkel", "off_by_owner", "heat", "off"),
                "claudias_room": ("mode", "keypad", "claudia", "would_restore", "heat", "off"),
                "bathroom": ("mode", "ha", "kristine", "would_restore", "heat", "off"),
            },
        )

    def test_switching_back_on_is_noted_and_needs_nothing(self):
        h = self.h
        h.report("kristines_room", state="off")
        h.report("kristines_room", state="heat")
        self.assertEqual(h.effects("kristines_room"), ["would_restore", "none"])
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.0)

    def test_a_preset_change_is_classified_too(self):
        h = self.h
        h.ha.update_attrs(THERMOSTATS["kristines_room"], preset_mode="permanent_hold")
        h.tick(minutes=5)
        h.report("kristines_room", preset_mode="schedule")
        rec = self.one()
        self.assertEqual(
            (rec["kind"], rec["source"], rec["effect"], rec["old"], rec["new"]),
            ("preset", "keypad", "would_restore", "permanent_hold", "schedule"),
        )

    def test_what_changed_while_the_thermostat_was_gone_is_not_a_signal(self):
        h = self.h
        h.report("kristines_room", state="unavailable")
        h.report("kristines_room", state="heat", temperature=17.0)
        self.assertEqual(h.signals(), [])
        h.report("kristines_room", temperature=17.5)
        self.assertEqual(h.effects("kristines_room"), ["nudge"])

    def test_a_report_that_changes_nothing_is_no_signal(self):
        h = self.h
        h.report("kristines_room", current_temperature=22.9)
        h.report("kristines_room", temperature=15.0)
        h.report("kristines_room", hvac_action="heating")
        self.assertEqual(h.signals(), [])

    def test_a_change_reported_twice_is_one_signal(self):
        h = self.h
        h.report("kristines_room", temperature=15.5)
        h.report("kristines_room", temperature=15.5)
        self.assertEqual(h.effects("kristines_room"), ["nudge"])

    def test_a_button_press_and_a_keypad_change_in_one_burst_are_one_step(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.clock.advance(minutes=2)
        h.report("kristines_room", temperature=15.5)
        self.assertEqual(h.effects("kristines_room"), ["nudge", "nudge_collapsed"])
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)

    def test_only_climate_calls_on_the_managed_thermostats_are_remembered(self):
        h = self.h
        h.service_call(
            "family",
            "set_temperature",
            MIKKEL_ID,
            entity="climate.bedroom_thermostat",
            temperature=20,
        )
        self.assertEqual(len(h.app._calls), 0)
        h.service_call("family", "set_temperature", MIKKEL_ID, temperature=20)
        self.assertEqual(len(h.app._calls), 1)
        for junk in (None, {}, {"service_data": []}, "x"):
            h.app._on_service_call("call_service", junk, {})
        self.assertEqual(len(h.app._calls), 1)
        self.assertEqual([m for lvl, m in h.logs if lvl == "ERROR"], [])

    def test_the_zone_sensor_shows_the_last_signal(self):
        h = self.h
        h.service_call(
            "kristines_room", "set_temperature", None, "automation-context", temperature=18.0
        )
        h.report("kristines_room", temperature=18.0)
        h.app._evaluate_pending({})
        a = h.attrs("kristines_room")
        self.assertEqual(
            a["last_signal"],
            {
                "at": "2026-10-17T12:00:00+02:00",
                "who": "automation",
                "source": "automation",
                "direction": "warmer",
                "effect": "would_restore",
            },
        )
        assert_publishable(self, a)
        self.assertNotIn("last_signal", h.attrs("family"))

    def test_a_signal_without_a_direction_leaves_it_out(self):
        h = self.h
        h.report("kristines_room", state="off")
        h.app._evaluate_pending({})
        self.assertNotIn("direction", h.attrs("kristines_room")["last_signal"])

    def test_an_unreadable_identity_lookup_does_not_lose_the_signal(self):
        h = self.h

        def boom(entity, **kw):
            if entity.startswith("person."):
                raise RuntimeError("no such entity")
            return h.ha.get_state(entity, **kw)

        h.app.get_state = boom
        h.app._identity_at = -10_000.0
        h.report("kristines_room", temperature=15.5)
        self.assertEqual(h.effects("kristines_room"), ["nudge"])

    def test_who_an_account_is_comes_from_the_person_entities_and_the_yaml(self):
        h = self.h
        users = h.app._identity_for(h.clock.now.timestamp()).users
        self.assertEqual(
            users,
            {
                MIKKEL_ID: "mikkel",
                CLAUDIA_ID: "claudia",
                KRISTINE_ID: "kristine",
                AGENT_ID: "agent",
            },
        )
        h.ha.set("person.kristine", "home", {"user_id": STRANGER_ID})
        h.clock.advance(minutes=11)
        users = h.app._identity_for(h.clock.now.timestamp()).users
        self.assertEqual(users[STRANGER_ID], "kristine")

    def test_without_an_app_account_in_the_yaml_the_app_is_an_unknown_account(self):
        h = Harness(self, appdaemon_user_id="").season()
        self.assertTrue(any("appdaemon_user_id not configured" in m for m in h.warnings()))
        h.service_call("kristines_room", "set_temperature", APP_ID, temperature=18.0)
        h.report("kristines_room", temperature=18.0)
        self.assertEqual(self.one_of(h, "kristines_room")["source"], "unknown")


class SeasonStart(unittest.TestCase):
    def pressed(self):
        h = Harness(self)
        h.tick()
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.press(CLAUDIA_COLDER, CLAUDIA_ID)
        return h

    def test_the_nudge_survives_the_season_and_resets_when_the_next_one_starts(self):
        h = self.pressed()
        h.tick(minutes=5)
        self.assertEqual(
            (h.app._zones["kristines_room"].nudge, h.app._zones["claudias_room"].nudge), (0.5, -0.5)
        )
        h.season()
        for zone in ("kristines_room", "claudias_room"):
            self.assertEqual(h.app._zones[zone].nudge, 0.0, zone)
        resets = [r for r in h.signals() if r["kind"] == "season"]
        self.assertEqual(
            [(r["zone"], r["effect"], r["nudge"]) for r in resets],
            [
                ("kristines_room", "nudge_reset", [0.5, 0.0]),
                ("claudias_room", "nudge_reset", [-0.5, 0.0]),
            ],
        )
        h.app._evaluate_pending({})
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 20.5)

    def test_a_season_that_is_already_on_when_the_app_starts_resets_nothing(self):
        h = Harness(self)
        h.season()
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.tick(minutes=5)
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)

    def test_a_state_that_does_not_remember_the_season_resets_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        Path(args["state_file"]).write_text(
            json.dumps({"zones": {"kristines_room": {"nudge": 0.5}}})
        )
        h = Harness(self, args=args, forecast=False).season()
        h.tick(minutes=5)
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)

    def test_a_home_assistant_restart_is_not_a_new_season(self):
        h = Harness(self).season()
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.ha.set("input_boolean.heating_season", "unavailable")
        h.tick(minutes=5)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick(minutes=5)
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)

    def test_a_season_that_started_while_the_app_was_down_is_noticed_after_the_restart(self):
        h = self.pressed()
        h.tick(minutes=5)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        h2.ha.entities = h.ha.entities
        h2.season()
        self.assertEqual(h2.app._zones["kristines_room"].nudge, 0.0)

    def test_a_season_that_was_on_before_and_after_the_restart_resets_nothing(self):
        h = Harness(self).season()
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.tick(minutes=5)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        h2.ha.entities = h.ha.entities
        h2.tick(minutes=5)
        self.assertEqual(h2.app._zones["kristines_room"].nudge, 0.5)


class SignalState(unittest.TestCase):
    def test_nudges_signals_and_the_window_survive_a_restart(self):
        h = Harness(self).season()
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.service_call("claudias_room", "set_temperature", None, "p", temperature=18.0)
        h.report("claudias_room", temperature=18.0)
        h.tick(minutes=1)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        h2.ha.entities = h.ha.entities
        rt = h2.app._zones["kristines_room"]
        self.assertEqual((rt.nudge, rt.nudge_dir, rt.nudge_at is not None), (0.5, 1, True))
        self.assertEqual(len(h2.app._signals), 2)
        self.assertEqual(rt.last_signal["effect"], "nudge")
        self.assertEqual(h2.app._zones["claudias_room"].last_signal["effect"], "would_restore")
        self.assertIsNone(h2.app._zones["family"].last_signal)
        h2.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(h.effects("kristines_room")[-1], "nudge_collapsed")
        h2.tick()
        self.assertEqual(h2.attrs("kristines_room")["last_signal"]["effect"], "nudge_collapsed")

    def test_only_the_last_fifty_signals_are_kept(self):
        h = Harness(self).season()
        for i in range(60):
            h.clock.advance(minutes=6)
            h.press(KRISTINE_WARMER if i % 2 == 0 else KRISTINE_COLDER, KRISTINE_ID)
        self.assertEqual(len(h.app._signals), 50)
        h.tick()
        self.assertEqual(len(json.loads((h.tmp / "state.json").read_text())["signals"]), 50)

    def test_junk_in_the_new_state_keys_is_survivable(self):
        for junk in (
            {"signals": "x", "alerts": 3, "season_on": "yes"},
            {"signals": [1, "x", None], "alerts": [], "season_on": 1},
            {"zones": {"kristines_room": {"nudge_at": "x", "nudge_dir": "up", "nudge": 0.5}}},
            {"signals": [{"zone": "nowhere", "at": 1}], "alerts": {"runs": {"family": 5}}},
        ):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            args = shipped_args(tmp)
            Path(args["state_file"]).write_text(json.dumps(junk))
            h = Harness(self, args=args, forecast=False).season()
            h.tick(minutes=5)
            self.assertEqual(h.attrs("family")["mode"], "hold", junk)
            self.assertEqual([m for lvl, m in h.logs if lvl == "ERROR"], [], junk)


class AlertsEndToEnd(unittest.TestCase):
    COLD = {"stale": "false", "sun_hit": "false", "window_open": "false"}

    def pushes(self, h):
        """The pushes since the last call: (title, message, target)."""
        h.run_tasks()
        calls = h.notifier.notify.await_args_list
        fresh, h.pushes_seen = calls[h.pushes_seen :], len(calls)
        return [(c.kwargs["title"], c.kwargs["message"], c.kwargs["target"]) for c in fresh]

    def cold_kristine(self, h, value="18.4"):
        h.ha.set("sensor.kristines_room_feel", value, self.COLD)

    def test_a_cold_room_pushes_to_the_owner_after_half_an_hour(self):
        h = Harness(self)
        self.cold_kristine(h)
        h.season()
        for _ in range(5):
            h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        h.tick(minutes=5)
        self.assertEqual(
            self.pushes(h),
            [("Heating: cold room", "Kristines room 18.4 °C for 30 min (target 20.5)", "mikkel")],
        )
        self.assertEqual(
            h.last("sensor.heating_controller")["attributes"]["alerts_active"],
            ["cold:kristines_room"],
        )
        (rec,) = h.log_lines("alert")
        self.assertEqual(
            (rec["key"], rec["zone"], rec["event"]),
            ("cold:kristines_room", "kristines_room", "start"),
        )
        self.assertEqual(rec["ts"], "2026-10-17T12:30:00+02:00")

    def test_it_pushes_once_per_episode_and_clears_quietly(self):
        h = Harness(self)
        self.cold_kristine(h)
        h.season()
        for _ in range(20):
            h.tick(minutes=5)
        self.assertEqual(len(self.pushes(h)), 1)
        self.cold_kristine(h, "20.2")
        h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual([r["event"] for r in h.log_lines("alert")], ["start", "clear"])
        self.assertEqual(
            h.last("sensor.heating_controller")["attributes"]["alerts_active"], ["<none>"]
        )

    def test_a_second_cold_spell_inside_six_hours_is_logged_but_not_pushed(self):
        h = Harness(self)
        self.cold_kristine(h)
        h.season()
        for _ in range(6):
            h.tick(minutes=5)
        self.assertEqual(len(self.pushes(h)), 1)
        self.cold_kristine(h, "20.2")
        h.tick(minutes=5)
        self.cold_kristine(h, "18.4")
        for _ in range(8):
            h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(
            [r["event"] for r in h.log_lines("alert")], ["start", "clear", "suppressed"]
        )
        self.assertEqual(
            h.last("sensor.heating_controller")["attributes"]["alerts_active"],
            ["cold:kristines_room"],
        )

    def test_nothing_is_pushed_in_a_normal_season_day(self):
        h = Harness(self).season()
        h.at(0, 0)
        for _ in range(24 * 12):
            h.tick()
            h.clock.advance(minutes=5)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(h.log_lines("alert"), [])

    def test_the_living_room_monitor_in_heat_is_reported_also_outside_the_season(self):
        h = Harness(self)
        h.ha.set("climate.living_room_thermostat", "heat", {"current_temperature": 22.0})
        h.tick()
        self.assertEqual(
            self.pushes(h),
            [
                (
                    "Heating: zone 8 in heat",
                    "Zone 8 has no actuator: heat mode runs the pump",
                    "mikkel",
                )
            ],
        )
        h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        h.ha.set("climate.living_room_thermostat", "off", {"current_temperature": 22.0})
        h.tick(minutes=5)
        self.assertEqual([r["event"] for r in h.log_lines("alert")], ["start", "clear"])

    def test_a_monitor_that_is_not_configured_or_unreadable_says_nothing(self):
        h = Harness(self)
        h.ha.set("climate.living_room_thermostat", "unavailable")
        h.tick()
        self.assertEqual(self.pushes(h), [])
        h2 = Harness(self, unmanaged={})
        h2.ha.set("climate.living_room_thermostat", "heat")
        h2.tick()
        self.assertEqual(self.pushes(h2), [])

    def test_no_supply_clamp_configured_means_nothing_reads_it_and_nothing_is_said(self):
        h = Harness(self)
        asked = []
        original = h.app.get_state
        h.app.get_state = lambda entity_id=None, **kw: (
            asked.append(entity_id),
            original(entity_id, **kw),
        )[1]
        h.ha.set(
            THERMOSTATS["bathroom"],
            "heat",
            {"current_temperature": 22.8, "temperature": 15.0, "hvac_action": "heating"},
        )
        h.season()
        for _ in range(8):
            h.tick(minutes=5)
        self.assertNotIn("", asked)
        self.assertEqual(self.pushes(h), [])
        stats = h.app._house["2026-10-17"]
        self.assertNotIn("supply", stats)
        self.assertNotIn("return", stats)

    def test_a_cold_supply_while_a_zone_calls_pushes_when_a_clamp_is_configured(self):
        h = Harness(self, supply_temp_entity="sensor.supply", return_temp_entity="sensor.return")
        h.ha.set("sensor.supply", "24.0")
        h.ha.set("sensor.return", "22.0")
        h.ha.set(
            THERMOSTATS["bathroom"],
            "heat",
            {"current_temperature": 22.8, "temperature": 15.0, "hvac_action": "heating"},
        )
        h.season()
        for _ in range(5):
            h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        h.tick(minutes=5)
        self.assertEqual(
            self.pushes(h),
            [("Heating: no hot water", "No hot water from the building (supply 24 °C)", "mikkel")],
        )
        stats = h.app._house["2026-10-17"]
        self.assertEqual((stats["supply"][1], stats["return"][1]), (7, 7))
        self.assertEqual(stats["supply"][0] / 7, 24.0)

    def test_a_clamp_reading_that_is_nonsense_is_ignored(self):
        h = Harness(self, supply_temp_entity="sensor.supply")
        h.ha.set(
            THERMOSTATS["bathroom"],
            "heat",
            {"current_temperature": 22.8, "temperature": 15.0, "hvac_action": "heating"},
        )
        for value in ("unavailable", "-300", "999", "abc"):
            h.ha.set("sensor.supply", value)
            self.assertIsNone(h.app._clamp_temp("sensor.supply"), value)
        h.ha.set("sensor.supply", "45.5")
        self.assertEqual(h.app._clamp_temp("sensor.supply"), 45.5)

    def test_a_pump_that_is_off_while_a_zone_calls_for_four_hours_pushes(self):
        h = Harness(self)
        h.ha.set("sensor.floor_heating_relay_power", "1.6")
        h.season(tick=False)
        for i in range(49):
            h.ha.set(
                THERMOSTATS["bathroom"],
                "heat",
                {"current_temperature": 22.8, "temperature": 15.0, "hvac_action": "heating"},
            )
            h.ha.set(
                "sensor.bathroom_floor_thermometer_temperature", str(round(20.0 + 0.05 * i, 2))
            )
            h.tick()
            h.clock.advance(minutes=5)
        self.assertEqual(
            self.pushes(h),
            [("Heating: pump off", "Bathroom has called 4 h, wiring-centre pump is off", "mikkel")],
        )

    def test_three_uneven_nights_push_the_floor_alert(self):
        clock = Clock(datetime(2026, 10, 17, 2, 0, tzinfo=TZ))
        h = Harness(self, clock=clock)
        h.season(tick=False)
        for day in (17, 18, 19):
            h.at(2, 0, day=day)
            h.fetch()
            while h.clock.now.hour < 6:
                h.ha.set(LIVING_FLOOR, "20.0")
                h.ha.set(DINING_FLOOR, "21.8")
                h.tick()
                h.clock.advance(minutes=5)
            h.tick()
            if day < 19:
                self.assertEqual(self.pushes(h), [], day)
        self.assertEqual(
            self.pushes(h),
            [("Heating: uneven floors", "Living/dining floors uneven: 1.8 °C", "mikkel")],
        )

    def test_a_zone_without_room_temperature_for_three_hours_pushes(self):
        h = Harness(self)
        for feel in FEELS["family"]:
            h.ha.set(feel, "unavailable")
        old = h.clock.now - timedelta(minutes=200)
        h.ha.set(
            THERMOSTATS["family"],
            "heat",
            {"current_temperature": None, "temperature": 15.0},
            updated=old,
        )
        for floor in (LIVING_FLOOR, DINING_FLOOR):
            h.ha.set(floor, "21.0", updated=old)
        h.season()
        self.assertEqual(h.attrs("family")["mode"], "fallback")
        for _ in range(35):
            h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        h.tick(minutes=5)
        self.assertEqual(
            self.pushes(h),
            [
                (
                    "Heating: no room temperature",
                    "Family has no usable room temperature and runs on its constant target",
                    "mikkel",
                )
            ],
        )

    def test_a_forecast_that_has_gone_stale_pushes_once(self):
        h = Harness(self).season()
        h.at(23, 55)
        h.tick()
        self.assertEqual(self.pushes(h), [])
        h.at(0, 5, day=18)
        h.tick()
        self.assertEqual(
            self.pushes(h),
            [
                (
                    "Heating: forecast stale",
                    "The forecast is stale: no sun cuts until it is refreshed",
                    "mikkel",
                )
            ],
        )
        h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        h.fetch()
        h.tick(minutes=5)
        self.assertEqual(
            h.last("sensor.heating_controller")["attributes"]["alerts_active"], ["<none>"]
        )

    def test_no_forecast_at_all_is_stale_after_twelve_hours_not_before(self):
        h = Harness(self, forecast=False).season()
        h.tick(minutes=11 * 60)
        self.assertEqual(self.pushes(h), [])
        h.tick(minutes=61)
        self.assertEqual(
            [m for _t, m, _g in self.pushes(h)],
            ["The forecast is stale: no sun cuts until it is refreshed"],
        )

    def test_every_push_goes_to_the_owner_only(self):
        h = Harness(self)
        self.cold_kristine(h)
        h.ha.set("climate.living_room_thermostat", "heat")
        h.ha.set("input_boolean.party", "on")
        h.season()
        for _ in range(8):
            h.tick(minutes=5)
        pushes = self.pushes(h)
        self.assertGreaterEqual(len(pushes), 2)
        self.assertEqual({target for _t, _m, target in pushes}, {"mikkel"})

    def test_alert_episodes_survive_a_restart_without_a_second_push(self):
        h = Harness(self)
        self.cold_kristine(h)
        h.season()
        for _ in range(6):
            h.tick(minutes=5)
        self.assertEqual(len(self.pushes(h)), 1)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        h2.ha.entities = h.ha.entities
        for _ in range(4):
            h2.tick(minutes=5)
        self.assertEqual(self.pushes(h2), [])
        self.assertEqual(h2.app._alerts.active(), ["cold:kristines_room"])

    def test_a_failing_push_is_only_a_warning(self):
        h = Harness(self)
        h.notifier.notify = AsyncMock(side_effect=RuntimeError("no phone"))
        h.ha.set("climate.living_room_thermostat", "heat")
        h.tick()
        h.run_tasks()
        self.assertTrue(any("push failed" in m for m in h.warnings()))
        self.assertEqual(h.app._alerts.active(), ["zone8_heat"])

    def test_event_driven_evaluations_do_not_feed_the_alerts(self):
        h = Harness(self)
        h.ha.set("climate.living_room_thermostat", "heat")
        h.app._evaluate_pending({})
        self.assertEqual(h.log_lines("alert"), [])
        h.tick()
        self.assertEqual(len(h.log_lines("alert")), 1)

    def test_the_log_lines_for_alerts_are_ascii_safe(self):
        h = Harness(self)
        self.cold_kristine(h)
        h.season()
        for _ in range(7):
            h.tick(minutes=5)
        for _lvl, msg in h.logs:
            msg.encode("ascii")
        for f in (h.tmp / "heating_log").glob("*"):
            f.read_text().encode("ascii")


class SeasonHarness:
    """Helpers for the season advice tests. The day under test is DAY of MONTH 2026 and its
    morning runs on real ticks from 05:00; the mornings before it are handed to the advisor."""

    MONTH = 10
    DAY = 19
    COLD = {"stale": "false", "sun_hit": "false", "window_open": "false"}

    def set_clock(self, h, hour, minute=0, day=None):
        h.clock.now = datetime(2026, self.MONTH, day or self.DAY, hour, minute, tzinfo=TZ)
        return h

    def harness(self, **kw):
        clock = Clock(datetime(2026, self.MONTH, self.DAY, 5, 0, tzinfo=TZ))
        return Harness(self, clock=clock, forecast=False, **kw)

    def forecast(self, h, temp=7.0, day=None):
        """A four-day forecast from midnight of the day with every hour at `temp`."""
        midnight = int(datetime(2026, self.MONTH, day or self.DAY, tzinfo=TZ).timestamp())
        n = 96
        h.payload = {
            "hourly": {
                "time": [midnight + 3600 * i for i in range(n)],
                "temperature_2m": [temp] * n,
                "wind_speed_10m": [10.0] * n,
                "shortwave_radiation": [0.0] * n,
                "cloud_cover": [50] * n,
            }
        }
        h.fetch()

    def cold(self, h, family=20.8, claudia=20.9, kristine=None):
        """The air of the advised zones; the family room is its living room."""
        for feel in FEELS["family"]:
            h.ha.set(feel, str(family), self.COLD)
        h.ha.set("sensor.claudias_room_feel", str(claudia), self.COLD)
        if kristine is not None:
            h.ha.set("sensor.kristines_room_feel", str(kristine), self.COLD)

    def before(self, h, family=20.9, claudia=20.9, kristine=None):
        """The morning before, as the advisor would have kept it."""
        day = f"2026-{self.MONTH:02d}-{self.DAY - 1:02d}"
        advisor = h.app._advisor
        advisor.note_season(False, day)
        for i in range(36):
            advisor.observe("family", day, 5 * 60 + 5 * i, family)
            advisor.observe("claudias_room", day, 5 * 60 + 5 * i, claudia)
            if kristine is not None:
                advisor.observe("kristines_room", day, 5 * 60 + 5 * i, kristine)

    def morning(self, h, until=(8, 0), start=(5, 0), day=None):
        """Five-minute ticks from `start` up to and including `until`."""
        self.set_clock(h, *start, day=day)
        while (h.clock.now.hour, h.clock.now.minute) <= until:
            h.tick()
            h.clock.advance(minutes=5)

    def pushes(self, h):
        h.run_tasks()
        calls = h.notifier.notify.await_args_list
        fresh, h.pushes_seen = calls[h.pushes_seen :], len(calls)
        return [(c.kwargs["title"], c.kwargs["message"], c.kwargs["target"]) for c in fresh]

    def sensor(self, h):
        return h.last("sensor.heating_controller")["attributes"]

    def restarted(self, h):
        again = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        again.ha.entities = h.ha.entities
        return again

    def silent(self, h):
        for name in (
            "call_service",
            "turn_on",
            "turn_off",
            "toggle",
            "select_option",
            "set_value",
            "set_textvalue",
        ):
            getattr(h.app, name).assert_not_called()
        self.assertEqual(self.sensor(h)["zones_live"], ["<none>"])


class SeasonStartEndToEnd(SeasonHarness, unittest.TestCase):
    MESSAGE = (
        "Family room 20.8 °C this morning (target 21.5), Claudia's room 20.9. "
        "Next 3 days 7 °C. Time to turn the heat on?"
    )

    def cold_house(self):
        h = self.harness()
        self.forecast(h)
        self.cold(h)
        self.before(h, kristine=22.2)
        return h

    def test_cold_mornings_and_a_cold_forecast_push_the_owner_at_eight(self):
        h = self.cold_house()
        self.morning(h, until=(7, 55))
        self.assertEqual(self.pushes(h), [])
        a = self.sensor(h)
        self.assertEqual(a["season_advice"], "none")
        self.assertNotIn("season_advice_day", a)
        self.morning(h, start=(8, 0), until=(8, 0))
        self.assertEqual(self.pushes(h), [("Heating on?", self.MESSAGE, "mikkel")])

    def test_the_sensor_and_the_log_show_what_it_judged(self):
        h = self.cold_house()
        self.morning(h)
        a = self.sensor(h)
        self.assertEqual(
            (a["season_advice"], a["season_advice_day"], a["season_forecast_mean"]),
            ("start", "2026-10-19", "7.0"),
        )
        self.assertNotIn("season_advice_reason", a)
        self.assertEqual(
            a["season_morning_means"],
            {
                "family": ["20.8", "20.9"],
                "kristines_room": ["22.2", "22.2"],
                "claudias_room": ["20.9", "20.9"],
            },
        )
        assert_publishable(self, a)
        (rec,) = h.log_lines("season_advice")
        self.assertEqual(rec["ts"], "2026-10-19T08:00:00+02:00")
        self.assertEqual(
            (rec["advice"], rec["pushed"], rec["failed"], rec["forecast_mean"]),
            ("start", True, ["family", "claudias_room"], 7.0),
        )
        self.assertEqual(rec["mornings"]["family"], [20.8, 20.9])
        self.assertEqual(
            rec["limits"], {"family": 21.0, "kristines_room": 20.3, "claudias_room": 21.0}
        )
        self.assertEqual(rec["message"], self.MESSAGE)
        self.assertEqual(h.app._advisor.snapshot()["asked"], {"start": "2026-10-19"})

    def test_it_judges_once_a_day(self):
        h = self.cold_house()
        self.morning(h, until=(8, 30))
        self.assertEqual(len(self.pushes(h)), 1)
        self.set_clock(h, 15)
        h.tick()
        h.tick(minutes=5)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(len(h.log_lines("season_advice")), 1)

    def test_only_the_rooms_that_failed_are_named_and_an_aired_room_is_not_a_cold_one(self):
        h = self.cold_house()
        h.ha.set("binary_sensor.living_room_window_contact", "on")
        self.morning(h)
        self.assertEqual(
            self.pushes(h),
            [
                (
                    "Heating on?",
                    "Claudia's room 20.9 °C this morning (target 21.5). "
                    "Next 3 days 7 °C. Time to turn the heat on?",
                    "mikkel",
                )
            ],
        )
        self.assertEqual(self.sensor(h)["season_morning_means"]["family"], ["n/a", "20.9"])

    def test_the_family_room_is_judged_on_the_living_room_alone(self):
        h = self.harness()
        self.forecast(h)
        self.before(h, family=20.6)
        h.ha.set("sensor.living_room_feel", "20.6", self.COLD)
        h.ha.set("sensor.dining_room_feel", "22.4", self.COLD)
        h.ha.set("sensor.kitchen_feel", "22.4", self.COLD)
        self.morning(h)
        (push,) = self.pushes(h)
        self.assertIn("Family room 20.6 °C this morning (target 21.5)", push[1])
        self.assertEqual(self.sensor(h)["season_morning_means"]["family"], ["20.6", "20.6"])

    def test_kristine_is_judged_closer_to_her_target(self):
        h = self.harness()
        self.forecast(h)
        self.before(h, family=22.0, claudia=22.0, kristine=20.2)
        self.cold(h, family=22.0, claudia=22.0, kristine=20.2)
        self.morning(h)
        self.assertEqual(
            self.pushes(h),
            [
                (
                    "Heating on?",
                    "Kristine's room 20.2 °C this morning (target 20.5). "
                    "Next 3 days 7 °C. Time to turn the heat on?",
                    "mikkel",
                )
            ],
        )
        h = self.harness()
        self.forecast(h)
        self.before(h, family=21.2, claudia=21.2, kristine=21.4)
        self.cold(h, family=21.2, claudia=21.2, kristine=21.4)
        self.morning(h)
        self.assertEqual(self.pushes(h), [])

    def test_the_limit_is_the_target_helper_not_the_yaml_base(self):
        h = self.harness()
        self.forecast(h)
        h.ha.set("input_number.heating_target_family", "22.0")
        self.cold(h, family=21.2, claudia=22.0)
        self.before(h, family=21.2, claudia=22.0)
        self.morning(h)
        (push,) = self.pushes(h)
        self.assertIn("Family room 21.2 °C this morning (target 22.0)", push[1])

    def test_warm_mornings_and_a_mild_forecast_ask_nothing(self):
        h = self.harness()
        self.forecast(h)
        self.before(h, family=22.0, claudia=22.0)
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice"], "none")
        self.assertEqual(self.sensor(h)["season_advice_reason"], "warm_enough")
        h = self.harness()
        self.forecast(h, temp=12.0)
        self.cold(h)
        self.before(h)
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice_reason"], "forecast_mild")
        self.assertEqual(h.log_lines("season_advice"), [])

    def test_without_a_forecast_it_waits_and_asks_when_one_arrives(self):
        h = self.harness()
        self.cold(h)
        self.before(h)
        self.morning(h, until=(9, 0))
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(h.log_lines("season_advice"), [])
        self.forecast(h)
        h.tick()
        self.assertEqual(len(self.pushes(h)), 1)

    def test_a_forecast_that_does_not_reach_three_days_ahead_is_no_forecast(self):
        h = self.harness()
        h.payload = copy.deepcopy(FIXTURE)
        h.fetch()
        self.cold(h)
        self.before(h)
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["forecast_ok"], "true")

    def test_only_room_sensors_that_read_count(self):
        h = self.cold_house()
        for feel in FEELS["family"] + FEELS["claudias_room"]:
            h.ha.set(feel, "20.8", {"stale": "true"})
        self.morning(h)
        self.assertNotIn("2026-10-19", h.app._advisor.snapshot()["mornings"].get("family", {}))
        self.assertEqual(self.pushes(h), [])
        for feel in FEELS["family"] + FEELS["claudias_room"]:
            h.ha.set(feel, "unavailable")
        self.morning(h, start=(5, 0))
        self.assertNotIn("2026-10-19", h.app._advisor.snapshot()["mornings"].get("family", {}))

    def test_an_impossible_room_reading_is_not_a_sample(self):
        h = self.cold_house()
        for feel in FEELS["family"]:
            h.ha.set(feel, "0.0", self.COLD)
        self.morning(h)
        self.assertNotIn("2026-10-19", h.app._advisor.snapshot()["mornings"].get("family", {}))

    def test_a_morning_with_the_heat_on_is_not_kept_and_asks_nothing(self):
        h = self.cold_house()
        h.ha.set("input_boolean.heating_season", "on")
        self.morning(h)
        self.assertNotIn("2026-10-19", h.app._advisor.snapshot()["mornings"].get("family", {}))
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice"], "none")
        self.assertEqual(self.sensor(h)["season_advice_reason"], "outside_dates")

    def test_an_unreadable_season_switch_judges_nothing(self):
        h = self.cold_house()
        h.ha.set("input_boolean.heating_season", "unavailable")
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertNotIn("2026-10-19", h.app._advisor.snapshot()["mornings"].get("family", {}))

    def test_event_driven_evaluations_do_not_weigh_a_reading_twice(self):
        h = self.cold_house()
        self.set_clock(h, 6)
        h.tick()
        h.app._evaluate_pending({})
        h.app._evaluate_pending({})
        self.assertEqual(h.app._advisor.snapshot()["mornings"]["family"]["2026-10-19"][1], 1)

    def test_the_morning_means_survive_a_restart_in_the_middle_of_the_morning(self):
        h = self.harness()
        self.forecast(h)
        self.before(h, claudia=22.0)
        self.cold(h, family=20.4, claudia=22.0)
        self.morning(h, until=(6, 30))
        self.cold(h, family=21.2, claudia=22.0)
        h2 = self.restarted(h)
        self.forecast(h2)
        self.morning(h2, start=(6, 35), until=(8, 0))
        self.assertEqual(
            self.pushes(h2),
            [
                (
                    "Heating on?",
                    "Family room 20.8 °C this morning (target 21.5). "
                    "Next 3 days 7 °C. Time to turn the heat on?",
                    "mikkel",
                )
            ],
        )

    def test_a_restart_keeps_the_day_used_up_and_the_ask_quiet_for_three_days(self):
        h = self.cold_house()
        self.morning(h)
        self.assertEqual(len(self.pushes(h)), 1)
        h2 = self.restarted(h)
        h2.tick()
        h2.tick(minutes=5)
        self.assertEqual(self.pushes(h2), [])
        self.set_clock(h2, 5, day=20)
        self.forecast(h2, day=20)
        self.morning(h2, day=20)
        self.assertEqual(self.pushes(h2), [])
        quiet = h.log_lines("season_advice")[-1]
        self.assertEqual(
            (quiet["day"], quiet["advice"], quiet["pushed"], quiet["reason"]),
            ("2026-10-20", "start", False, "asked_recently"),
        )
        a = self.sensor(h2)
        self.assertEqual(
            (a["season_advice"], a["season_advice_reason"]), ("start", "asked_recently")
        )
        for day in ("2026-10-21", "2026-10-22"):
            for i in range(36):
                h2.app._advisor.observe("family", day, 5 * 60 + 5 * i, 20.8)
        self.set_clock(h2, 8, day=22)
        self.forecast(h2, day=22)
        h2.tick()
        self.assertEqual(len(self.pushes(h2)), 1)

    def test_switching_the_season_on_ends_the_ask(self):
        h = self.cold_house()
        self.morning(h)
        self.assertEqual(len(self.pushes(h)), 1)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        self.assertEqual(self.sensor(h)["season_advice"], "none")
        self.assertEqual(h.app._advisor.snapshot()["asked"], {})

    def test_without_the_block_there_is_no_advisor_and_nothing_changes(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        del args["season_advisor"]
        h = self.harness(args=args)
        self.forecast(h)
        self.cold(h)
        self.morning(h)
        self.assertIsNone(h.app._advisor)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual([k for k in self.sensor(h) if k.startswith("season_")], [])
        self.assertNotIn("season_advisor", json.loads((tmp / "state.json").read_text()))

    def test_a_fault_in_the_advisor_is_logged_and_the_controller_carries_on(self):
        h = self.cold_house()

        def boom(inp):
            raise RuntimeError("boom")

        h.app._advisor.evaluate = boom
        self.morning(h, until=(8, 5))
        self.assertTrue(any(lvl == "ERROR" and "season advice failed" in m for lvl, m in h.logs))
        self.assertEqual(h.attrs("family")["mode"], "off_season")
        self.assertEqual(self.sensor(h)["last_tick"], "2026-10-19T08:05:00+02:00")

    def test_the_log_lines_and_files_are_ascii_safe(self):
        h = self.cold_house()
        self.morning(h)
        for _lvl, msg in h.logs:
            msg.encode("ascii")
        for f in (h.tmp / "heating_log").glob("*"):
            f.read_text().encode("ascii")
        self.assertTrue(any("season advice: start" in m for _lvl, m in h.logs))

    def test_it_never_calls_a_service(self):
        h = self.cold_house()
        self.morning(h)
        self.silent(h)
        self.assertEqual(len(self.pushes(h)), 1)


class SeasonStopEndToEnd(SeasonHarness, unittest.TestCase):
    MONTH = 3
    DAY = 26
    DAYS = [f"2026-03-{d}" for d in range(19, 26)]

    def spring(self, calling=None, since="2025-10-26", drop=()):
        """The season on since `since`, and the 19th to the 25th closed by the real rollup. The
        zones in `calling` ask for heat for that many seconds on the day given; the days in
        `drop` are left out as if the controller had not seen them."""
        h = self.harness()
        h.ha.set("input_boolean.heating_season", "on")
        h.ha.set("sensor.kristines_room_feel", "21.6", self.COLD)
        h.ha.set("sensor.claudias_room_feel", "22.9", self.COLD)
        self.forecast(h, temp=7.0)
        h.app._advisor.note_season(True, since)
        for zone, rt in h.app._zones.items():
            rt.days = {}
            for day in self.DAYS:
                if day not in drop:
                    call_s = (calling or {}).get((zone, day), 0.0)
                    rt.days[day] = hp.ZoneDay(day, {"ticks": 288, "call_s": call_s})
        h.app._rollup_if_due(h.clock.now)
        return h

    def test_a_week_without_much_heat_and_a_mild_outlook_ask_whether_it_can_go_off(self):
        h = self.spring()
        self.assertEqual(len(h.app._zones["family"].daily), 7)
        self.morning(h)
        self.assertEqual(
            self.pushes(h),
            [
                (
                    "Heating off?",
                    "Heat ran 0.0 h a day last week, rooms 22-23 °C. "
                    "Next 3 days 7 °C. Turn the heat off?",
                    "mikkel",
                )
            ],
        )
        (rec,) = h.log_lines("season_advice")
        self.assertEqual(rec["ts"], "2026-03-26T08:00:00+01:00")
        self.assertEqual(
            (rec["advice"], rec["pushed"], rec["call_hours"]), ("stop", True, [0.0] * 7)
        )
        a = self.sensor(h)
        self.assertEqual((a["season_advice"], a["season_forecast_mean"]), ("stop", "7.0"))
        self.assertNotIn("season_morning_means", a)
        assert_publishable(self, a)

    def test_blips_are_allowed_but_three_busy_days_are_not(self):
        two = {("family", "2026-03-20"): 3 * 3600.0, ("family", "2026-03-23"): 5 * 3600.0}
        h = self.spring(two)
        self.morning(h)
        self.assertEqual(len(self.pushes(h)), 1)
        three = {**two, ("claudias_room", "2026-03-24"): 2.5 * 3600.0}
        h = self.spring(three)
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice_reason"], "heat_wanted")

    def test_the_limit_is_all_the_zones_together(self):
        days = ("2026-03-20", "2026-03-22", "2026-03-24")
        h = self.spring({("family", d): 1800.0 for d in days})  # 0.5 h a day
        self.morning(h)
        self.assertEqual(len(self.pushes(h)), 1)
        h = self.spring({(zone, d): 1800.0 for zone in ZONES for d in days})  # 2.5 h a day
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice_reason"], "heat_wanted")

    def test_a_cold_outlook_keeps_the_heat_on(self):
        h = self.spring()
        self.forecast(h, temp=4.0)
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice_reason"], "forecast_cool")
        h = self.spring()
        self.forecast(h, temp=5.0)
        self.morning(h)
        self.assertEqual(len(self.pushes(h)), 1)

    def test_a_season_only_days_old_is_not_asked_about(self):
        h = self.spring(since="2026-03-20")
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice_reason"], "season_young")

    def test_days_the_controller_did_not_see_are_not_idle_ones(self):
        h = self.spring(drop=("2026-03-20", "2026-03-22"))
        self.morning(h)
        self.assertEqual(len(self.pushes(h)), 1)
        h = self.spring(drop=("2026-03-20", "2026-03-22", "2026-03-24"))
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_advice_reason"], "no_data")

    def test_a_day_the_rollup_only_closes_at_the_first_tick_is_counted_by_that_tick(self):
        # two busy days leave exactly five idle ones, the last of them still open
        h = self.spring({("family", "2026-03-20"): 10800.0, ("family", "2026-03-22"): 10800.0})
        for rt in h.app._zones.values():
            rt.daily = [r for r in rt.daily if r["day"] != "2026-03-25"]
            rt.days["2026-03-25"] = hp.ZoneDay("2026-03-25", {"ticks": 288, "call_s": 0.0})
        self.morning(h, start=(9, 0), until=(9, 0))
        self.assertEqual(len(self.pushes(h)), 1)

    def test_the_ask_is_not_repeated_for_three_days_not_even_after_a_restart(self):
        h = self.spring()
        self.morning(h)
        self.assertEqual(len(self.pushes(h)), 1)
        h2 = self.restarted(h)
        pushed = []
        for day in (27, 28, 29):
            for rt in h2.app._zones.values():
                rt.daily.append({"day": f"2026-03-{day - 1}", "ticks": 288, "call_hours": 0.0})
            self.set_clock(h2, 8, day=day)
            self.forecast(h2, day=day)
            h2.tick()
            pushed.append(len(self.pushes(h2)))
        self.assertEqual(pushed, [0, 0, 1])
        self.assertEqual(h.log_lines("season_advice")[1]["reason"], "asked_recently")

    def test_it_never_calls_a_service(self):
        h = self.spring()
        self.morning(h)
        self.silent(h)
        self.assertEqual(len(self.pushes(h)), 1)


class EverythingStaysShadow(unittest.TestCase):
    def test_signals_and_alerts_never_call_a_service(self):
        h = Harness(self)
        h.ha.set("climate.living_room_thermostat", "heat")
        h.ha.set("sensor.kristines_room_feel", "18.4", {"stale": "false", "sun_hit": "false"})
        h.season()
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.press(CLAUDIA_COLDER, STRANGER_ID)
        h.helper("input_number.heating_target_family", 21.5, 23.0, KRISTINE_ID)
        h.helper(KRISTINE_TARGET, 20.5, 22.0, MIKKEL_ID)
        h.service_call("kristines_room", "set_temperature", MIKKEL_ID, temperature=16.0)
        h.report("kristines_room", temperature=16.0)
        h.report("claudias_room", state="off")
        h.report("family", temperature=23.0)
        for _ in range(10):
            h.tick(minutes=5)
        self.assertGreaterEqual(len(h.signals()), 6)
        self.assertTrue(h.app._alerts.active())
        for name in (
            "call_service",
            "turn_on",
            "turn_off",
            "toggle",
            "select_option",
            "set_value",
            "set_textvalue",
        ):
            getattr(h.app, name).assert_not_called()
        self.assertEqual(h.last("sensor.heating_controller")["state"], "shadow")
        self.assertEqual(
            h.last("sensor.heating_controller")["attributes"]["zones_live"], ["<none>"]
        )

    def test_the_new_modules_import_nothing_that_could_write(self):
        for name in ("heating_signals.py", "heating_alerts.py", "heating_season.py"):
            source = (APPS / name).read_text()
            self.assertIsNone(
                re.search(r"^\s*(import|from)\s+(appdaemon|requests|os|subprocess)", source, re.M),
                name,
            )


if __name__ == "__main__":
    unittest.main()
