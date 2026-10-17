# tests/test_heating_controller.py - HeatingController (shadow mode): wiring, publishing, the
# decision log, forecast handling, the plan end to end and the state file. Same __new__ +
# monkeypatched-callables harness as test_salus_health.py, with a small fake Home Assistant
# behind get_state. The planning rules themselves are tested in test_heating_plan.py.
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
    "family": ["sensor.living_room_feel", "sensor.dining_room_feel"],
    "kristines_room": ["sensor.kristines_room_feel"],
    "claudias_room": ["sensor.claudias_room_feel"],
    "bathroom": ["sensor.bathroom_feel"],
    "guest_bathroom": ["sensor.guest_bathroom_feel"],
}
HOME_LAT, HOME_LON = 55.6841, 12.5666  # a made-up point in central Copenhagen
# Helpers that exist in Home Assistant for this controller (increment 1).
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
}


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
    ha.set("sensor.kitchen_feel", "22.0", {"air_band": "good"})
    ha.set("sensor.gw2000a_outdoor_temperature", "11.3")
    ha.set("weather.forecast_home", "cloudy", {"temperature": 11.7})
    ha.set("sensor.floor_heating_relay_power", "1.6")


class Harness:
    """A HeatingController with initialize() run for real against the fake house."""

    def __init__(self, testcase, args=None, clock=None, with_house=True, forecast=True):
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
        self.notifier = MagicMock()
        self.notifier.notify = AsyncMock()
        self.payload = copy.deepcopy(FIXTURE)
        self.fetch_error = None

        app = hc.HeatingController.__new__(hc.HeatingController)
        app.args = args if args is not None else shipped_args(self.tmp)
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

    def test_helper_entities_are_ones_that_exist(self):
        wanted = {self.cfg.season_entity, self.cfg.bedroom.ceiling_entity}
        wanted.update(zc.target_entity for zc in self.cfg.zones.values())
        wanted.update(
            [self.cfg.party.start_entity, self.cfg.party.end_entity, self.cfg.party.switch_entity]
        )
        self.assertLessEqual(wanted, HELPERS)

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
        h.app.listen_event.assert_called_once_with(h.app._on_plugin_started, "plugin_started")
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

    def test_one_of_two_family_rooms_is_enough(self):
        self.h.ha.set("sensor.dining_room_feel", "unavailable")
        self.h.ha.set("sensor.living_room_feel", "22.6", {"stale": "false"})
        self.h.tick()
        self.assertEqual(self.h.attrs("family")["control_air"], 22.6)

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
            "heat",
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
            {"current_temperature": 22.8, "temperature": 15.0},
            updated=old,
        )
        self.h.ha.set("sensor.living_room_floor_thermometer_temperature", "21.0", updated=old)
        self.h.tick()
        a = self.h.attrs("family")
        self.assertEqual((a["mode"], a["control_air_source"]), ("fallback", "none"))
        self.assertEqual(a["setpoint_now"], 22.0)
        self.assertTrue(a["stale_inputs"])

    def test_an_impossible_reading_is_ignored(self):
        self.h.ha.set("sensor.living_room_feel", "88.0", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "22.0", {"stale": "false"})
        self.h.tick()
        self.assertEqual(self.h.attrs("family")["control_air"], 22.0)

    def test_a_jump_inside_ten_minutes_is_held_back(self):
        self.h.tick()
        self.h.ha.set("sensor.living_room_feel", "26.5", {"stale": "false"})
        self.h.ha.set("sensor.dining_room_feel", "26.5", {"stale": "false"})
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
        h.ha.set("sensor.kitchen_feel", "22.0", {"air_band": "stuffy"})
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
        h.ha.set("sensor.kitchen_feel", "22.0", {"air_band": "poor"})
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


if __name__ == "__main__":
    unittest.main()
