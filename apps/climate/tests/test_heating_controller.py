# tests/test_heating_controller.py - HeatingController (shadow mode): wiring, publishing, the
# decision log, forecast handling, the plan end to end, the nudge buttons, helper and thermostat
# signals, the alerts, the season advice and the state file. Same __new__ + monkeypatched-callables
# harness as test_salus_health.py, with a small fake Home Assistant behind get_state. The planning
# rules are tested in test_heating_plan.py, the signal rules in test_heating_signals.py, the
# alert rules in test_heating_alerts.py and the season rules in test_heating_season.py.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import re
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
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
    "family": ["sensor.living_room_feel", "sensor.dining_room_feel", "sensor.kitchen_feel"],
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
PRESS_TIME = "2026-10-01T10:00:00+00:00"


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
    """A quiet October afternoon: season off, every sensor fresh, air 22.2, thermostats 22.8."""
    ha.set("input_boolean.heating_season", "off")
    ha.set("input_number.heating_bedroom_ceiling", "21.0")
    for zone, value in zip(ZONES, ("21.5", "21.5", "21.5", "22.0", "20.0")):
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
        lo, hi = {"bathroom": (19, 25), "guest_bathroom": (18, 24)}.get(zone, (19, 24))
        ha.set(f"number.{stem}_min_heat_setpoint", lo)
        ha.set(f"number.{stem}_max_heat_setpoint", hi)
        for feel in FEELS[zone]:
            ha.set(feel, "22.2", {"stale": "false", "sun_hit": "false", "window_open": "false"})
        ha.set(f"sensor.{zone}_floor_thermometer_temperature", "21.0")
    ha.set("sensor.living_room_floor_thermometer_temperature", "21.0")
    ha.set("sensor.dining_room_floor_thermometer_temperature", "21.0")
    ha.set("climate.living_room_thermostat", "off", {"current_temperature": 22.0})
    ha.set("person.mikkel", "home", {"user_id": MIKKEL_ID})
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
        self, testcase, args=None, clock=None, with_house=True, forecast=True, **overrides
    ):
        self.tmp = Path(tempfile.mkdtemp(prefix="heating-test-"))
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

        app = hc.HeatingController.__new__(hc.HeatingController)
        app.args = args if args is not None else {**shipped_args(self.tmp), **overrides}
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
        shadow = AssertionError("shadow mode must not call a service")
        for name in (
            "call_service",
            "turn_on",
            "turn_off",
            "toggle",
            "select_option",
            "set_value",
            "set_textvalue",
        ):
            setattr(app, name, MagicMock(side_effect=shadow))
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
        files = sorted((self.tmp / "heating_log").glob("decisions-*.jsonl"))
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
        self.assertEqual([z[n].base_c for n in ZONES], [21.5, 21.5, 21.5, 22.0, 20.0])
        self.assertEqual([z[n].lower_c for n in ZONES], [19.0, 19.0, 19.0, 19.0, 18.0])
        self.assertEqual([z[n].upper_c for n in ZONES], [24.0, 24.0, 24.0, 25.0, 24.0])
        self.assertEqual(z["family"].protected_min_c, 20.5)
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
        self.assertEqual(cfg.user_ids, {KRISTINE_ID: "kristine"})
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

    def test_the_family_room_is_one_zone_of_living_room_dining_room_and_kitchen(self):
        rooms = ("sensor.living_room_feel", "sensor.dining_room_feel", "sensor.kitchen_feel")
        self.assertEqual(self.cfg.zones["family"].control_air, rooms)
        self.assertEqual(self.cfg.season_advisor.rooms["family"], rooms)

    def test_the_advisor_judges_every_zone_on_the_air_the_controller_plans_by(self):
        a = self.cfg.season_advisor
        for zone in a.zones:
            self.assertEqual(a.rooms[zone], self.cfg.zones[zone].control_air, zone)

    def test_family_profile_is_minus_half_nights_and_mornings_plus_half_evenings(self):
        periods = {(p.start, p.end): p.offset for p in self.cfg.zones["family"].profile.periods}
        self.assertEqual(periods, {(23 * 60, 10 * 60): -0.5, (17 * 60, 23 * 60): 0.5})

    def test_bathroom_heats_in_the_morning_window_only(self):
        zc = self.cfg.zones["bathroom"]
        self.assertEqual(zc.profile.lead_hours, 0)
        self.assertTrue(zc.profile.outside_is_lower_limit)
        self.assertEqual([(p.start, p.end) for p in zc.profile.periods], [(6 * 60, 9 * 60)])

    def test_only_the_bathroom_is_held_back_for_the_bedroom(self):
        guarded = [n for n, zc in self.cfg.zones.items() if zc.guard.enabled]
        self.assertEqual(guarded, ["bathroom"])
        self.assertEqual(self.cfg.zones["bathroom"].guard.cap, "lower_limit")

    def test_sun_cuts_only_where_the_glass_looks_at_the_sun(self):
        enabled = [n for n, zc in self.cfg.zones.items() if zc.sun_cut.enabled]
        self.assertEqual(enabled, ["family", "claudias_room"])
        self.assertEqual(self.cfg.zones["claudias_room"].sun_cut.az_min, 180.0)

    def test_only_the_family_zone_ships_a_weather_offset(self):
        shipped = {
            n: zc.weather_offset for n, zc in self.cfg.zones.items() if zc.weather_offset.enabled
        }
        self.assertEqual(shipped, {"family": hp.WeatherOffsetConfig(True, 0.85, 0.22, 1.5)})

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


class ShadowOnly(unittest.TestCase):
    def test_the_module_has_no_call_to_any_service(self):
        source = (APPS / "heating_controller.py").read_text()
        for needle in (
            "call_service",
            ".turn_on(",
            ".turn_off(",
            ".toggle(",
            "select_option",
            "set_value",
            "set_temperature",
            "set_hvac_mode",
            "set_preset_mode",
        ):
            self.assertNotIn(needle, source, needle)

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
        self.assertIn((h.app._on_service_call, "call_service", {"domain": "climate"}), events)
        for button in BUTTONS.values():
            self.assertIn((h.app._on_button, "state_changed", {"entity_id": button}), events)
        owned = {e["entity_id"] for cb, name, e in events if cb == h.app._on_helper}
        self.assertEqual(
            owned,
            {
                "input_boolean.heating_season",
                "input_number.heating_bedroom_ceiling",
                "input_boolean.party",
                "input_datetime.heating_party_start",
                "input_datetime.heating_party_end",
            }
            | {f"input_number.heating_target_{z}" for z in ZONES},
        )
        self.assertEqual(len(events), 1 + 1 + len(BUTTONS) + len(owned))
        self.assertTrue(any("shadow mode" in m for _lvl, m in h.logs))

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
        self.assertEqual(fam["attributes"]["setpoint_now"], 22.0)
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
        # 12:00: the family target is the base, the others are flat, the bathroom is below its window
        want = {
            "family": (21.5, 22.0),
            "kristines_room": (21.5, 21.5),
            "claudias_room": (21.5, 21.5),
            "bathroom": (19.0, 19.0),
            "guest_bathroom": (20.0, 20.0),
        }
        for zone, (state, setpoint) in want.items():
            self.assertEqual(h.zone(zone)["state"], state, zone)
            self.assertEqual(h.attrs(zone)["setpoint_now"], setpoint, zone)

    def test_the_first_plan_has_a_timeline_and_a_next_change(self):
        h = Harness(self)
        h.tick()
        a = h.attrs("family")
        self.assertEqual(
            [(b["start"], b["end"], b["setpoint"]) for b in a["day_blocks"]],
            [("12:00", "13:00", "22.0"), ("13:00", "19:00", "22.5"), ("19:00", "24:00", "21.5")],
        )
        self.assertEqual(a["next_change_at"], "2026-10-17T13:00:00+02:00")
        self.assertEqual(a["next_setpoint"], 22.5)
        self.assertEqual(a["trim"], "+0.50")
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
        self.assertEqual(h.attrs("family")["setpoint_now"], 22.0)

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
            (a["mode"], a["plan_mode"], a["setpoint_now"]), ("device_offline", "hold", 21.5)
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

    def test_the_family_air_is_the_mean_of_living_room_dining_room_and_kitchen(self):
        self.h.ha.set("sensor.living_room_feel", "22.0", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "22.6", {"stale": "false"})
        self.h.ha.set("sensor.kitchen_feel", "21.4", {"stale": "false"})
        self.h.tick()
        self.assertEqual(self.h.attrs("family")["control_air"], 22.0)

    def test_one_of_three_family_rooms_is_enough(self):
        self.h.ha.set("sensor.dining_room_feel", "unavailable")
        self.h.ha.set("sensor.kitchen_feel", "22.0", {"stale": "true"})
        self.h.ha.set("sensor.living_room_feel", "22.6", {"stale": "false"})
        self.h.tick()
        self.assertEqual(self.h.attrs("family")["control_air"], 22.6)
        self.assertEqual(
            self.h.attrs("family")["stale_inputs"],
            ["control_air:sensor.dining_room_feel", "control_air:sensor.kitchen_feel"],
        )

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
        self.h.ha.set("sensor.living_room_floor_thermometer_temperature", "21.0", updated=old)
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["mode"], a["control_air_source"]), ("fallback", "none"))
        self.assertEqual(a["setpoint_now"], 22.0)
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

    def test_unchanged_online_temperature_is_a_usable_fallback_and_trim_sample(self):
        old = self.h.clock.now - timedelta(hours=6)
        self.h.ha.set(
            THERMOSTATS["family"], "heat",
            {"current_temperature": 22.8, "temperature": 15.0}, updated=old,
        )
        app, rt = self.h.app, self.h.app._zones["family"]
        now_ts = self.h.clock.now.timestamp()
        rt.setpoint_changed_ts = old.timestamp()
        reading = app._read_zone(rt, now_ts, False)
        self.assertAlmostEqual(
            app._trim_sample(rt, types.SimpleNamespace(mode=hp.MODE_HOLD), reading, False, now_ts),
            0.6,
        )
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

    def test_lost_device_with_cached_temperature_is_not_a_reading_or_trim_sample(self):
        old = self.h.clock.now - timedelta(minutes=200)
        self.h.ha.set(
            THERMOSTATS["family"], "unavailable",
            {"current_temperature": 22.8, "temperature": 15.0}, updated=old,
        )
        app, rt = self.h.app, self.h.app._zones["family"]
        now_ts = self.h.clock.now.timestamp()
        rt.setpoint_changed_ts = old.timestamp()
        reading = app._read_zone(rt, now_ts, False)
        self.assertIsNone(reading["therm_temp"])
        self.assertFalse(reading["therm_usable"])
        self.assertIn(f"thermostat:{THERMOSTATS['family']}", reading["stale"])
        self.assertIsNone(
            app._trim_sample(rt, types.SimpleNamespace(mode=hp.MODE_HOLD), reading, False, now_ts)
        )

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
        self.h.ha.set(
            "sensor.living_room_floor_thermometer_temperature", "21.0",
            updated=self.h.clock.now - timedelta(minutes=65.6),
        )
        reading = self.h.app._read_zone(
            self.h.app._zones["family"], self.h.clock.now.timestamp(), False
        )
        self.assertEqual(reading["air_source"], "floor")
        self.assertFalse(reading["air_fresh"])

    def test_an_impossible_reading_is_ignored(self):
        self.h.ha.set("sensor.living_room_feel", "88.0", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "22.0", {"stale": "false"})
        self.h.ha.set("sensor.kitchen_feel", "22.0", {"stale": "false"})
        self.h.tick()
        self.assertEqual(self.h.attrs("family")["control_air"], 22.0)

    def test_a_jump_inside_ten_minutes_is_held_back(self):
        self.h.tick()
        for feel in FEELS["family"]:
            self.h.ha.set(feel, "26.5", {"stale": "false"})
        self.h.tick(minutes=5)
        self.assertEqual(self.h.attrs("family")["control_air"], 22.2)
        self.h.tick(minutes=5)
        self.assertEqual(self.h.attrs("family")["control_air"], 26.5)


class DeviceLimits(unittest.TestCase):
    def test_matching_limits_stay_quiet(self):
        h = Harness(self)
        h.tick()
        self.assertEqual(h.warnings(), [])

    def test_factory_limits_are_reported_once_per_thermostat(self):
        h = Harness(self)
        for thermostat in THERMOSTATS.values():
            stem = thermostat.split(".")[1]
            h.ha.set(f"number.{stem}_min_heat_setpoint", 5)
            h.ha.set(f"number.{stem}_max_heat_setpoint", 35)
        h.tick()
        h.tick(minutes=5)
        found = [m for m in h.warnings() if "thermostat limits" in m]
        self.assertEqual(len(found), 5)
        self.assertEqual(
            h.attrs("family")["upper_limit"], 24.0
        )  # the configured limits still apply

    def test_a_stricter_device_limit_wins(self):
        h = Harness(self)
        h.ha.set("number.bathroom_thermostat_min_heat_setpoint", 19.5)
        h.ha.set("number.family_room_thermostat_max_heat_setpoint", 22.0)
        h.tick()
        self.assertEqual(h.attrs("bathroom")["lower_limit"], 19.5)
        self.assertEqual(h.attrs("bathroom")["setpoint_now"], 19.5)
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
        h.at(3, 0, day=18)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "sun_cut")
        h.fetch_error = OSError("down")
        h.at(15, 0, day=18)
        h.tick()
        self.assertEqual(h.last("sensor.heating_controller")["attributes"]["forecast_ok"], "false")
        h.at(3, 0, day=19)
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


class SunCut(unittest.TestCase):
    def season(self, **kw):
        h = Harness(self, **kw)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(2, 55, day=18)
        h.fetch()
        return h

    def test_the_sunny_day_cuts_the_family_zone_in_the_small_hours(self):
        h = self.season()
        h.at(3, 0, day=18)
        h.tick()
        a = h.attrs("family")
        self.assertEqual(a["mode"], "sun_cut")
        # aim: the night target 21.0 at 07:00, capped at 20.5, plus the trim of 0.5
        self.assertEqual(a["setpoint_now"], 21.0)
        self.assertEqual(
            a["cut_window"], ["2026-10-18T01:00:00+02:00", "2026-10-18T16:00:00+02:00"]
        )
        self.assertIn("Sun cut until 16:00", a["reasons"][0])
        self.assertEqual(a["sun_credit_h"], "2.0")
        self.assertEqual(a["budget_today_h"], "1.6")
        self.assertEqual(a["budget_dull_h"], "5.7")

    def test_the_cut_ends_with_its_window(self):
        h = self.season()
        h.at(16, 5, day=18)
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")

    def test_before_the_window_the_cut_is_announced_but_not_running(self):
        h = self.season()
        h.at(0, 30, day=18)
        h.tick()
        a = h.attrs("family")
        self.assertEqual(a["mode"], "hold")
        self.assertEqual(a["cut_window"][0], "2026-10-18T01:00:00+02:00")

    def test_the_dull_day_is_not_cut(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        self.assertEqual(h.attrs("family")["mode"], "hold")
        # today's two bright hours are not worth a cut; the window shown is tomorrow's
        self.assertEqual(h.attrs("family")["cut_window"][0], "2026-10-18T01:00:00+02:00")

    def test_claudias_window_opens_later_because_her_glass_looks_west(self):
        h = self.season()
        h.at(4, 30, day=18)
        h.tick()
        self.assertEqual(h.attrs("claudias_room")["mode"], "hold")
        h.at(5, 5, day=18)
        h.tick()
        a = h.attrs("claudias_room")
        self.assertEqual(a["mode"], "sun_cut")
        self.assertEqual(a["setpoint_now"], 21.0)  # her protected minimum, base minus half a degree

    def test_the_other_zones_are_never_cut(self):
        h = self.season()
        h.at(10, 0, day=18)
        h.tick()
        for zone in ("kristines_room", "bathroom", "guest_bathroom"):
            self.assertNotEqual(h.attrs(zone)["mode"], "sun_cut")

    def test_full_trust_cuts_a_borderline_day_that_half_trust_leaves_alone(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(4, 55)
        h.fetch()
        h.at(5, 0)
        h.tick()  # 17 October: ratio 0.44 at half trust
        self.assertEqual(h.attrs("family")["mode"], "hold")
        h.app.cfg = dataclasses.replace(h.app.cfg, sun_term_factor=1.0)  # 0.89 at full trust
        h.tick(minutes=5)
        self.assertEqual(h.attrs("family")["mode"], "sun_cut")


class WeatherOffsetEndToEnd(unittest.TestCase):
    """On a cold day the family zone asks its thermostat for more than the profile says."""

    COLD_PLAN = [
        "Aim 21.5: the target 4 h ahead",
        "Weather +0.5: expected duty 0.63",
        "Setpoint 22.5 (trim +0.50, weather +0.49)",
    ]

    @staticmethod
    def forecast(h, today, tomorrow):
        """Fetch a forecast with a flat temperature on 17 and on 18 October, and no sun."""
        hourly = h.payload["hourly"]
        temp = {17: today, 18: tomorrow}
        hourly["temperature_2m"] = [
            temp.get(hp.local_dt(t - 1800, TZ).day, tomorrow) for t in hourly["time"]
        ]
        hourly["shortwave_radiation"] = [0.0] * len(hourly["time"])
        h.fetch()

    def cold_house(self, today=-3.0, tomorrow=-3.0):
        h = Harness(self, forecast=False)
        h.ha.set("input_boolean.heating_season", "on")
        self.forecast(h, today, tomorrow)
        return h

    @staticmethod
    def decision(h, zone="family"):
        return [r for r in h.log_lines("decision") if r["zone"] == zone][-1]

    def test_a_cold_day_raises_the_family_setpoint_and_the_sensor_and_the_log_say_so(self):
        h = self.cold_house()
        h.tick()
        a, d = h.attrs("family"), self.decision(h)
        self.assertEqual((a["weather_offset"], d["weather_offset"]), ("+0.49", 0.49))
        self.assertEqual((d["aim"], d["raw"], d["setpoint"]), (21.5, 22.49, 22.5))
        self.assertEqual(a["setpoint_now"], 22.5)
        self.assertEqual(h.zone("family")["state"], 21.5)
        self.assertEqual(a["reasons"], self.COLD_PLAN)
        self.assertEqual(d["reasons"], self.COLD_PLAN)

    def test_the_plan_shows_the_raise_outside_the_season_too(self):
        h = self.cold_house()
        h.ha.set("input_boolean.heating_season", "off")
        h.tick()
        a = h.attrs("family")
        self.assertEqual(
            (a["mode"], a["plan_mode"], a["weather_offset"]), ("off_season", "hold", "+0.49")
        )

    def test_no_other_zone_is_raised(self):
        h = self.cold_house()
        h.tick()
        others = ZONES[1:]
        for zone in others:
            self.assertEqual(h.attrs(zone)["weather_offset"], "+0.00", zone)
            self.assertEqual(self.decision(h, zone)["weather_offset"], 0.0, zone)
        self.assertEqual([h.attrs(z)["setpoint_now"] for z in others], [21.5, 21.5, 19.0, 20.0])

    def test_a_mild_day_changes_nothing(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        a, d = h.attrs("family"), self.decision(h)
        self.assertEqual((a["weather_offset"], d["weather_offset"]), ("+0.00", 0.0))
        self.assertEqual(a["setpoint_now"], 22.0)
        self.assertEqual(
            a["reasons"], ["Aim 21.5: the target 4 h ahead", "Setpoint 22.0 (trim +0.50)"]
        )

    def test_the_day_at_the_lead_time_decides_which_forecast_counts(self):
        h = self.cold_house(today=11.0, tomorrow=-3.0)
        h.at(19, 55).tick()  # lead time 23:55, still today
        self.assertEqual(h.attrs("family")["weather_offset"], "+0.00")
        h.at(20, 5).tick()  # lead time 00:05, tomorrow
        a = h.attrs("family")
        self.assertEqual(a["weather_offset"], "+0.47")
        self.assertIn("Weather +0.5: expected duty 0.62", a["reasons"])

    def test_the_budget_reaches_the_lead_day_at_the_end_of_the_projection(self):
        h = self.cold_house()
        h.tick()
        days = [b["day"] for b in self.decision(h)["budget"]]
        self.assertEqual(days, ["2026-10-17", "2026-10-18", "2026-10-19"])

    def test_a_new_forecast_moves_the_projected_change_as_well(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        a = h.attrs("family")
        self.assertEqual(
            (a["next_change_at"], a["next_setpoint"]), ("2026-10-17T13:00:00+02:00", 22.5)
        )
        self.forecast(h, -3.0, -3.0)
        h.tick(minutes=1)
        a = h.attrs("family")
        self.assertEqual((a["setpoint_now"], a["next_setpoint"]), (22.5, 23.0))

    def test_an_airing_takes_the_raise_away_until_it_is_over(self):
        h = self.cold_house()
        h.tick()
        h.ha.set("binary_sensor.rooftop_door_1_contact", "on")
        h.tick(minutes=1)
        h.tick(minutes=2)
        a = h.attrs("family")
        self.assertEqual(
            (a["mode"], a["setpoint_now"], a["weather_offset"]), ("window", 19.0, "+0.00")
        )
        self.assertEqual(self.decision(h)["weather_offset"], 0.0)
        h.ha.set("binary_sensor.rooftop_door_1_contact", "off")
        h.tick(minutes=40)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["weather_offset"]), ("hold", "+0.49"))

    def test_without_a_usable_forecast_there_is_no_raise(self):
        h = Harness(self, forecast=False)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        self.assertEqual(h.attrs("family")["weather_offset"], "+0.00")
        h = self.cold_house()
        h.tick()
        self.assertEqual(h.attrs("family")["weather_offset"], "+0.49")
        h.fetch_error = OSError("down")
        h.at(0, 30, day=18).tick()  # 12.5 hours after the fetch
        self.assertEqual(h.last("sensor.heating_controller")["attributes"]["forecast_ok"], "false")
        a = h.attrs("family")
        self.assertEqual((a["weather_offset"], a["setpoint_now"]), ("+0.00", 21.5))
        self.assertEqual(self.decision(h)["weather_offset"], 0.0)


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
        self.assertEqual((a["mode"], a["setpoint_now"]), ("window", 19.0))
        d = [r for r in h.log_lines("decision") if r["zone"] == "family"][-1]
        self.assertEqual((d["gate"], d["would_write"]), ("priority:window_open", True))
        h.ha.set("binary_sensor.rooftop_door_1_contact", "off")
        h.ha.set("sensor.living_room_feel", "20.9", {"stale": "false"})
        h.ha.set("sensor.dining_room_feel", "20.9", {"stale": "false"})
        h.tick(minutes=7)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["setpoint_now"]), ("settle", 19.0))
        h.ha.set("sensor.living_room_feel", "22.0", {"stale": "false"})
        h.ha.set("sensor.dining_room_feel", "22.0", {"stale": "false"})
        h.tick(minutes=5)
        a = h.attrs("family")
        self.assertEqual((a["mode"], a["setpoint_now"]), ("hold", 22.0))
        d = [r for r in h.log_lines("decision") if r["zone"] == "family"][-1]
        self.assertEqual(d["gate"], "priority:window_restored")

    def test_a_long_opening_is_restored_at_once(self):
        h = self.h
        self.open_door()
        h.tick(minutes=3)
        h.tick(minutes=30)
        h.ha.set("binary_sensor.rooftop_door_1_contact", "off")
        h.tick(minutes=2)
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
        self.assertEqual((a["mode"], a["setpoint_now"]), ("party", 19.0))
        self.assertEqual(
            a["party_window"], ["2026-10-17T11:00:00+02:00", "2026-10-17T23:00:00+02:00"]
        )
        h.run_tasks()
        h.notifier.notify.assert_awaited_once()
        kw = h.notifier.notify.await_args.kwargs
        self.assertEqual(kw["target"], "mikkel")
        self.assertIn("would pause until 23:00", kw["message"])
        self.assertIn("Nothing is written", kw["message"])
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
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.evening(h, 21.4)
        self.assertTrue(h.app._loop.hold)
        self.assertEqual(h.log_lines("bedroom")[-1]["action"], "hold")
        h.at(7, 0, day=18)
        h.ha.set("sensor.bedroom_feel", "21.0", {"stale": "false", "window_open": "false"})
        h.tick()
        a = h.attrs("bathroom")
        self.assertEqual((a["mode"], a["setpoint_now"]), ("guard", 19.0))
        self.assertEqual(a["bedroom_hold_back"], "true")
        self.assertEqual(a["bedroom_evening_mean"], "21.4")
        self.assertEqual(h.attrs("family")["mode"], "hold")  # the family zone is never held back

    def test_a_cool_bedtime_gives_the_bathroom_its_morning_heat(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.app._loop.hold = True
        self.evening(h, 20.2)
        self.assertFalse(h.app._loop.hold)
        h.at(7, 0, day=18)
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


class TerminateAndEvents(unittest.TestCase):
    def test_terminate_saves_the_state_so_a_redeploy_keeps_the_day(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        h.app._zones["family"].trim = 0.7
        (h.tmp / "state.json").unlink()
        h.app.terminate()
        self.assertEqual(
            json.loads((h.tmp / "state.json").read_text())["zones"]["family"]["trim"], 0.7
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
        h = Harness(self)
        h.at(12, 0, day=25)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        blocks = h.attrs("family")["day_blocks"]
        self.assertEqual(blocks[-1]["end"], "24:00")
        self.assertEqual([b["setpoint"] for b in blocks], ["22.0", "22.5", "21.5"])
        self.assertEqual(h.attrs("family")["next_change_at"], "2026-10-25T13:00:00+01:00")

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
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(5, 50)
        h.tick()
        h.at(6, 5)
        h.tick()
        blocks = h.attrs("family")["day_blocks"]
        self.assertEqual(
            [(b["start"], b["end"], b["setpoint"]) for b in blocks],
            [
                ("05:50", "06:05", "21.5"),
                ("06:05", "13:00", "22.0"),
                ("13:00", "19:00", "22.5"),
                ("19:00", "24:00", "21.5"),
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
            (fam["mode"], fam["plan_mode"], fam["setpoint"]), ("off_season", "hold", 22.0)
        )
        self.assertEqual(fam["air_source"], "feel")
        self.assertEqual(fam["limits"], [19.0, 24.0])
        self.assertEqual(fam["trim"], 0.5)
        self.assertIn("budget", fam)
        self.assertEqual(fam["budget"][0]["day"], "2026-10-17")
        self.assertEqual(fam["reasons"][0], "Heating season is off: nothing would be written")

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
        self.assertEqual(h.attrs("family")["setpoint_now"], 22.0)


class StateFile(unittest.TestCase):
    def test_state_round_trips_through_a_restart(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.tick()
        h.tick(minutes=10)  # the plan has been stable long enough to be "written"
        rt = h.app._zones["family"]
        rt.trim = 0.65
        rt.nudge = 0.0
        h.tick(minutes=5)
        saved = json.loads((h.tmp / "state.json").read_text())
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["zones"]["family"]["trim"], 0.65)
        self.assertEqual(saved["zones"]["family"]["last_planned"], 22.0)
        h2 = Harness(self, args=h.app.args, clock=h.clock, with_house=False, forecast=False)
        self.assertEqual(h2.app._zones["family"].trim, 0.65)
        self.assertEqual(h2.app._zones["family"].last_planned, 22.0)
        self.assertIn("2026-10-17", h2.app._zones["family"].days)
        self.assertEqual(h2.app._zones["family"].blocks.day, "2026-10-17")

    def test_a_missing_file_gives_the_yaml_defaults(self):
        h = Harness(self, forecast=False)
        self.assertEqual(h.app._zones["family"].trim, 0.5)
        self.assertEqual(h.app._zones["bathroom"].trim, 0.0)
        self.assertEqual(h.warnings(), [])

    def test_a_corrupt_file_gives_the_yaml_defaults_and_a_warning(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        Path(args["state_file"]).write_text('{"zones": {"family": {"trim": ')
        h = Harness(self, args=args, forecast=False)
        self.assertEqual(h.app._zones["family"].trim, 0.5)
        self.assertTrue(any("state file unreadable" in m for m in h.warnings()))

    def test_valid_json_of_the_wrong_shape_is_survivable(self):
        for junk in (
            "[]",
            '"x"',
            '{"zones": [], "bedroom": 3, "house": 4, "notified": 5}',
            '{"zones": {"family": {"trim": "abc", "window": [], "days": {"2026-10-17": 5}, "bucket": 1}}}',
        ):
            tmp = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            args = shipped_args(tmp)
            Path(args["state_file"]).write_text(junk)
            h = Harness(self, args=args, forecast=False)
            h.tick()
            self.assertEqual(h.attrs("family")["setpoint_now"], 22.0, junk)

    def test_a_saved_trim_outside_the_range_is_clamped(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        args = shipped_args(tmp)
        Path(args["state_file"]).write_text(
            json.dumps({"zones": {"family": {"trim": 9.0, "nudge": 4.0}}})
        )
        h = Harness(self, args=args, forecast=False)
        self.assertEqual(h.app._zones["family"].trim, 1.5)
        self.assertEqual(h.app._zones["family"].nudge, 0.0)  # a zone without residents has no nudge

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
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 22.0)
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
        self.assertEqual(h.attrs("family")["setpoint_now"], 22.0)


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

    def test_the_trim_moves_halfway_to_the_measured_bias_at_03_00(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.run_day(h)
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        rt = h.app._zones["family"]
        self.assertEqual(rt.trim, 0.55)  # 0.5 + 0.5 * (0.6 - 0.5)
        daily = [r for r in h.log_lines("daily") if r["zone"] == "family"]
        self.assertEqual(len(daily), 1)
        rec = daily[0]
        self.assertEqual(
            (rec["day"], rec["trim_before"], rec["trim_after"]), ("2026-10-17", 0.5, 0.55)
        )
        self.assertEqual(rec["trim_bias"], 0.6)
        self.assertGreaterEqual(rec["trim_valid_hours"], 6)
        self.assertEqual(rec["call_hours"], 6.0)
        self.assertEqual(
            rec["in_band_pct"], 37.5
        )  # only the evening target (22.0) is within 0.5 of 22.2
        self.assertEqual(rec["ticks"], 24 * 12)
        house = [r for r in h.log_lines("daily") if r["zone"] == "_house"]
        self.assertEqual(len(house), 1)
        self.assertEqual(house[0]["ticks"], 24 * 12)

    def test_zones_whose_air_is_their_thermostat_stay_at_zero(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        h.at(0, 0)
        for _ in range(24 * 12):
            for zone in ("bathroom", "guest_bathroom", "kristines_room"):
                h.ha.set(
                    THERMOSTATS[zone], "heat", {"current_temperature": 22.8, "temperature": 15.0}
                )
                h.ha.set(FEELS[zone][0], "22.8", {"stale": "false", "sun_hit": "false"})
            h.tick()
            h.clock.advance(minutes=5)
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        for zone in ("bathroom", "guest_bathroom", "kristines_room"):
            self.assertEqual(h.app._zones[zone].trim, 0.0, zone)

    def test_off_season_ticks_teach_nothing(self):
        h = Harness(self)
        self.run_day(h)
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        self.assertEqual(h.app._zones["family"].trim, 0.5)

    def test_yesterday_shows_up_on_the_sensor_and_the_day_is_not_closed_twice(self):
        h = Harness(self)
        h.ha.set("input_boolean.heating_season", "on")
        self.run_day(h)
        h.at(3, 0, day=18)
        h.app._daily_rollup({})
        h.tick(minutes=5)
        y = h.attrs("family")["yesterday"]
        self.assertEqual((y["day"], y["trim"]), ("2026-10-17", 0.55))
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

    def test_a_thermostat_setpoint_change_resets_the_quiet_period(self):
        h = Harness(self, forecast=False)
        h.tick()
        rt = h.app._zones["family"]
        first = rt.setpoint_changed_ts
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
        self.assertEqual(rt.setpoint_changed_ts, first)
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
        self.assertEqual(rt.setpoint_changed_ts, h.clock.now.timestamp())


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
            (a["nudge"], a["setpoint_now"], a["profile_offset"]), ("+0.5", 22.0, "+0.0")
        )
        self.assertEqual(h.zone("kristines_room")["state"], 22.0)
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
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 22.5)
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
            len(h.app.listen_event.call_args_list), 1 + 1 + len(BUTTONS) + len(h.app._owned)
        )


class TargetHelpers(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self).season()

    def test_the_owner_changing_a_residents_target_resets_her_nudge(self):
        h = self.h
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        h.helper(KRISTINE_TARGET, 21.5, 22.0, MIKKEL_ID)
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
        h.helper(KRISTINE_TARGET, 21.5, 22.0, MIKKEL_ID)
        h.press(KRISTINE_WARMER, KRISTINE_ID)
        self.assertEqual(h.app._zones["kristines_room"].nudge, 0.5)

    def test_the_owner_changing_a_target_with_no_nudge_just_sets_it(self):
        h = self.h
        h.helper(KRISTINE_TARGET, 21.5, 22.0, MIKKEL_ID)
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
        h.helper(KRISTINE_TARGET, 21.5, 22.0, APP_ID)
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
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 22.0)

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
            ("ha", "mikkel", "would_set_target", 21.5, 22.0),
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
        self.assertEqual(h.zone("kristines_room")["state"], 21.5)

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
            users, {MIKKEL_ID: "mikkel", CLAUDIA_ID: "claudia", KRISTINE_ID: "kristine"}
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
        self.assertEqual(h.attrs("kristines_room")["setpoint_now"], 21.5)

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
            [("Heating: cold room", "Kristines room 18.4 °C for 30 min (target 21.5)", "mikkel")],
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
        h.ha.set(LIVING_FLOOR, "21.0", updated=old)
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
        """The air of the advised zones; the family room is its three rooms at one temperature."""
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
            rec["limits"], {"family": 21.0, "kristines_room": 21.3, "claudias_room": 21.0}
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

    def test_the_family_room_is_judged_on_living_room_dining_room_and_kitchen(self):
        h = self.harness()
        self.forecast(h)
        self.before(h, family=20.6)
        for feel, value in zip(FEELS["family"], ("20.6", "20.6", "22.4")):
            h.ha.set(feel, value, self.COLD)  # a warm kitchen: the joined room is 21.2 in all
        self.morning(h)
        self.assertEqual(self.pushes(h), [])
        self.assertEqual(self.sensor(h)["season_morning_means"]["family"], ["21.2", "20.6"])

    def test_a_kitchen_that_reads_stale_is_left_out_of_the_family_room(self):
        h = self.harness()
        self.forecast(h)
        self.before(h, family=20.6)
        for feel, value in zip(FEELS["family"], ("20.6", "20.6", "22.4")):
            h.ha.set(feel, value, dict(self.COLD, stale="true") if "kitchen" in feel else self.COLD)
        self.morning(h)
        (push,) = self.pushes(h)
        self.assertIn("Family room 20.6 °C this morning (target 21.5)", push[1])

    def test_kristine_is_judged_closer_to_her_target(self):
        h = self.harness()
        self.forecast(h)
        self.before(h, family=22.0, claudia=22.0, kristine=21.2)
        self.cold(h, family=22.0, claudia=22.0, kristine=21.2)
        self.morning(h)
        self.assertEqual(
            self.pushes(h),
            [
                (
                    "Heating on?",
                    "Kristine's room 21.2 °C this morning (target 21.5). "
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
        h.helper(KRISTINE_TARGET, 21.5, 22.0, MIKKEL_ID)
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

    def test_the_controller_names_no_service_to_call(self):
        lines = (APPS / "heating_controller.py").read_text().splitlines()
        self.assertEqual([ln for ln in lines if "call_service" in ln], [])
        for needle in (
            "set_temperature",
            "set_hvac_mode",
            "set_preset_mode",
            "number.set_value",
            "climate/",
        ):
            self.assertEqual([ln for ln in lines if needle in ln], [], needle)

    def test_the_new_modules_import_nothing_that_could_write(self):
        for name in ("heating_signals.py", "heating_alerts.py", "heating_season.py"):
            source = (APPS / name).read_text()
            self.assertIsNone(
                re.search(r"^\s*(import|from)\s+(appdaemon|requests|os|subprocess)", source, re.M),
                name,
            )


if __name__ == "__main__":
    unittest.main()
