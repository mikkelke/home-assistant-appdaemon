# tests/test_heating_plan.py - the floor-heating controller's planning logic (heating_plan.py):
# config parsing, comfort target and lead shift, allowed and aim by mode, a user who is away from
# home, budget and sun cut, party, bedroom loop, window tracker and the airing lockout, the call
# rule with the coast of the heat in transit and the drive setpoint, write bookkeeping and daily
# statistics. Pure logic, so no AppDaemon harness is needed.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import json
import math
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import heating_forecast as hf  # noqa: E402
import heating_plan as hp  # noqa: E402

TZ = ZoneInfo("Europe/Copenhagen")
DAY = date(2026, 10, 17)


def T(hour, minute=0, day=17, month=10):
    """Epoch of a local clock time in October 2026 (CEST until the 25th)."""
    return datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp()


FAMILY = {
    "thermostat": "climate.family_room_thermostat",
    "control_air": ["sensor.living_room_feel", "sensor.dining_room_feel", "sensor.kitchen_feel"],
    "floor": "sensor.living_room_floor_thermometer_temperature",
    "openings": [
        "binary_sensor.living_room_window_contact",
        "binary_sensor.rooftop_door_1_contact",
    ],
    "target_entity": "input_number.heating_target_family",
    "base_c": 21.5,
    "lower_limit_c": 19.0,
    "upper_limit_c": 24.0,
    "protected_min_c": 20.5,
    "profile": {
        "lead_hours": 4,
        "periods": [
            {"from": "23:00", "to": "10:00", "offset": -0.5},
            {"from": "17:00", "to": "23:00", "offset": 0.5},
        ],
    },
    "budget": {"a": 0.55, "b_om": -1.62, "c": 1.21, "d": -0.04},
    "sun_cut": {
        "enabled": True,
        "azimuth": [100, 260],
        "ghi_min": 150,
        "before_hours": 8,
        "end_before_hours": 1,
        "credit_ratio": 0.5,
    },
}
KRISTINE = {
    "thermostat": "climate.kristines_room_thermostat",
    "control_air": ["sensor.kristines_room_feel"],
    "floor": "sensor.kristines_room_floor_thermometer_temperature",
    "openings": ["binary_sensor.kristines_room_window_contact"],
    "target_entity": "input_number.heating_target_kristines_room",
    "base_c": 21.5,
    "lower_limit_c": 19.0,
    "upper_limit_c": 24.0,
    "protected_min_offset_c": -0.5,
    "profile": {"lead_hours": 4},
    "budget": {"changepoint": {"k": 0.17, "base_c": 15.5}},
    "residents": ["kristine"],
    "nudge": {"min": -1.0, "max": 1.0, "step": 0.5},
    "buttons": {"warmer": "input_button.a", "colder": "input_button.b"},
}
CLAUDIA = dict(
    KRISTINE,
    thermostat="climate.claudias_room_thermostat",
    control_air=["sensor.claudias_room_feel"],
    openings=["binary_sensor.rooftop_door_2_contact"],
    target_entity="input_number.heating_target_claudias_room",
    profile={"lead_hours": 6},
    budget={"a": 0.50, "b_om": -2.74, "c": 0, "d": 2.32},
    sun_cut={"enabled": True, "azimuth": [180, 290]},
    residents=["claudia"],
)
BATHROOM = {
    "thermostat": "climate.bathroom_thermostat",
    "control_air": ["sensor.bathroom_feel"],
    "openings": ["binary_sensor.bathroom_window_contact"],
    "target_entity": "input_number.heating_target_bathroom",
    "base_c": 22.0,
    "lower_limit_c": 19.0,
    "upper_limit_c": 25.0,
    "profile": {
        "lead_hours": 0,
        "outside": "lower_limit",
        "when_home": ["person.mikkel", "device_tracker.of9p"],
        "periods": [{"from": "06:00", "to": "09:00", "offset": 0}],
    },
    "budget": {"a": 0.64, "b_om": -2.47, "c": 0, "d": -1.24},
    "bedroom_guard": {"enabled": True, "cap": "lower_limit"},
}
GUEST = {
    "thermostat": "climate.guest_bathroom_thermostat",
    "control_air": ["sensor.guest_bathroom_feel"],
    "target_entity": "input_number.heating_target_guest_bathroom",
    "base_c": 20.0,
    "lower_limit_c": 18.0,
    "upper_limit_c": 24.0,
    "profile": {"lead_hours": 0},
}
ARGS = {
    "season_entity": "input_boolean.heating_season",
    "bedroom": {"ceiling_entity": "input_number.heating_bedroom_ceiling"},
    "party": {"zone": "family"},
    "zones": {
        "family": FAMILY,
        "kristines_room": KRISTINE,
        "claudias_room": CLAUDIA,
        "bathroom": BATHROOM,
        "guest_bathroom": GUEST,
    },
}
CFG = hp.parse_config(ARGS)


def inputs(zone="family", **kw):
    zc = CFG.zones[zone]
    base = dict(
        season_on=True,
        online=True,
        base=zc.base_c,
        nudge=0.0,
        lower_c=zc.lower_c,
        upper_c=zc.upper_c,
    )
    base.update(kw)
    return hp.ZoneInputs(**base)


def plan_at(ts, zone="family", **kw):
    return hp.plan(CFG.zones[zone], inputs(zone, **kw), ts, TZ)


def dw(t_mean, wind, ghi, day=DAY, hours=24):
    return hf.DayWeather(day, hours, t_mean, max(0.0, 17 - t_mean), wind, ghi)


class ClockHelpers(unittest.TestCase):
    def test_parse_clock(self):
        self.assertEqual(hp.parse_clock("00:00"), 0)
        self.assertEqual(hp.parse_clock("06:30"), 390)
        self.assertEqual(hp.parse_clock("23:59"), 1439)
        for bad in ("24:00", "6", "12:75", "ab:cd", 600, None):
            with self.assertRaises(hp.ConfigError, msg=repr(bad)):
                hp.parse_clock(bad)

    def test_parse_month_day(self):
        self.assertEqual(hp.parse_month_day("03-01"), (3, 1))
        self.assertEqual(hp.parse_month_day("09-30"), (9, 30))
        self.assertEqual(hp.parse_month_day("02-29"), (2, 29))
        for bad in ("13-01", "02-30", "00-10", "0301", "3", "2026-03-01", "ab-cd", 301, None):
            with self.assertRaises(hp.ConfigError, msg=repr(bad)):
                hp.parse_month_day(bad)

    def test_in_span_wraps_midnight(self):
        night = (hp.parse_clock("23:00"), hp.parse_clock("10:00"))
        self.assertTrue(hp.in_span(23 * 60, *night))
        self.assertTrue(hp.in_span(0, *night))
        self.assertTrue(hp.in_span(9 * 60 + 59, *night))
        self.assertFalse(hp.in_span(10 * 60, *night))
        self.assertFalse(hp.in_span(22 * 60 + 59, *night))
        self.assertTrue(hp.in_span(7 * 60, 6 * 60, 9 * 60))
        self.assertFalse(hp.in_span(9 * 60, 6 * 60, 9 * 60))

    def test_local_midnight_follows_the_clock_change(self):
        # 25 October 2026 has 25 hours: clocks go back at 03:00.
        a = hp.local_midnight_ts(date(2026, 10, 25), TZ)
        b = hp.local_midnight_ts(date(2026, 10, 26), TZ)
        self.assertEqual(b - a, 25 * 3600)

    def test_local_midnight_with_a_pytz_style_tzinfo(self):
        class Localizing(type(TZ)):  # a zoneinfo that also offers localize(), like pytz
            def localize(self, naive):
                return naive.replace(tzinfo=self)

        tz = Localizing("Europe/Copenhagen")
        self.assertEqual(hp.local_midnight_ts(DAY, tz), T(0))


class ConfigParsing(unittest.TestCase):
    def test_live_zones_default_to_empty_and_the_switch_is_configurable(self):
        self.assertEqual(CFG.live_zones, ())
        self.assertEqual(CFG.live_switch_entity, "input_boolean.heating_live")
        cfg = hp.parse_config(
            dict(ARGS, live_zones=["family"], live_switch_entity="input_boolean.heating_write")
        )
        self.assertEqual(cfg.live_zones, ("family",))
        self.assertEqual(cfg.live_switch_entity, "input_boolean.heating_write")

    def test_live_zones_must_be_a_list_of_configured_zone_names(self):
        for value in (["missing"], "family", {"family": True}, [1], [None]):
            with self.subTest(value=value), self.assertRaisesRegex(hp.ConfigError, "live_zones"):
                hp.parse_config(dict(ARGS, live_zones=value))

    def test_the_fixture_config_parses_with_the_decided_numbers(self):
        zc = CFG.zones["family"]
        self.assertEqual(
            (zc.base_c, zc.lower_c, zc.upper_c, zc.protected_min_c), (21.5, 19.0, 24.0, 20.5)
        )
        self.assertEqual(zc.profile.lead_hours, 4.0)
        self.assertEqual(len(zc.profile.periods), 2)
        self.assertEqual(CFG.zones["bathroom"].upper_c, 25.0)
        self.assertEqual(CFG.zones["guest_bathroom"].lower_c, 18.0)
        self.assertEqual(CFG.zones["claudias_room"].sun_cut.az_min, 180.0)
        self.assertFalse(CFG.zones["kristines_room"].sun_cut.enabled)
        self.assertEqual(CFG.zones["kristines_room"].nudge.maximum, 1.0)
        self.assertEqual(CFG.zones["kristines_room"].budget.changepoint_k, 0.17)

    def test_defaults(self):
        self.assertEqual(CFG.sun_term_factor, 0.5)
        self.assertEqual(CFG.writes_per_hour, 4)
        self.assertEqual(CFG.keypad_relock_minutes, 15)
        self.assertEqual(CFG.window.open_debounce_minutes, 2)
        self.assertEqual(CFG.bedroom.ceiling_default_c, 21.0)
        self.assertEqual(CFG.bedroom.release_below_c, 20.5)
        self.assertEqual(CFG.party.lead_hours, 8)

    def test_the_signal_and_alert_settings_have_defaults(self):
        self.assertEqual(CFG.owner, "mikkel")
        self.assertEqual((CFG.appdaemon_user_id, CFG.user_ids), ("", {}))
        self.assertEqual(
            (CFG.supply_temp_entity, CFG.return_temp_entity, CFG.living_monitor_entity),
            ("", "", ""),
        )
        a = CFG.alerts
        self.assertEqual((a.cold_c, a.cold_minutes, a.calling_hours), (19.0, 30.0, 4.0))
        self.assertEqual((a.no_pump_w, a.floor_rise_min_c, a.no_hot_water_c), (5.0, 0.1, 28.0))
        self.assertEqual((a.evenness_c, a.evenness_nights, a.repeat_hours), (1.5, 3, 6.0))
        self.assertEqual(CFG.zones["family"].evenness_floors, ())

    def test_the_signal_and_alert_settings_are_read(self):
        zones = dict(ARGS["zones"], family=dict(FAMILY, evenness_floors=["sensor.a", "sensor.b"]))
        cfg = hp.parse_config(
            dict(
                ARGS,
                zones=zones,
                owner="Mikkel",
                appdaemon_user_id="abc",
                user_ids={"5BEE": "Kristine"},
                supply_temp_entity="sensor.supply",
                return_temp_entity="sensor.return",
                unmanaged={"living_monitor": "climate.monitor"},
                alerts={"cold_c": 18.0, "cold_minutes": 10, "evenness_nights": 5},
            )
        )
        self.assertEqual((cfg.owner, cfg.appdaemon_user_id), ("mikkel", "abc"))
        self.assertEqual(cfg.user_ids, {"5BEE": "kristine"})
        self.assertEqual(
            (cfg.supply_temp_entity, cfg.return_temp_entity), ("sensor.supply", "sensor.return")
        )
        self.assertEqual(cfg.living_monitor_entity, "climate.monitor")
        self.assertEqual(
            (cfg.alerts.cold_c, cfg.alerts.cold_minutes, cfg.alerts.evenness_nights),
            (18.0, 10.0, 5),
        )
        self.assertEqual(cfg.alerts.calling_hours, 4.0)
        self.assertEqual(cfg.zones["family"].evenness_floors, ("sensor.a", "sensor.b"))

    def test_the_call_rule_has_defaults(self):
        c = CFG.call
        self.assertEqual((c.hysteresis_c, c.min_on_minutes, c.min_off_minutes), (0.2, 20.0, 20.0))
        self.assertEqual((c.start_every_minutes, c.start_window_minutes), (30.0, 6.0))
        self.assertEqual(c.high_margin_c, 2.0)
        self.assertEqual(c, hp.CallConfig())

    def test_the_call_rule_is_read_and_leaves_out_what_the_default_covers(self):
        cfg = hp.parse_config(
            dict(
                ARGS,
                call={
                    "hysteresis_c": 0.3,
                    "min_on_minutes": 15,
                    "min_off_minutes": 25,
                    "start_every_minutes": 60,
                    "start_window_minutes": 10,
                    "high_margin_c": 1.5,
                },
            )
        )
        self.assertEqual(cfg.call, hp.CallConfig(0.3, 15.0, 25.0, 60.0, 10.0, 1.5))
        part = hp.parse_config(dict(ARGS, call={"high_margin_c": 3}))
        self.assertEqual(part.call, hp.CallConfig(high_margin_c=3.0))
        for empty in (None, {}):
            self.assertEqual(hp.parse_config(dict(ARGS, call=empty)).call, hp.CallConfig())

    def test_a_bad_call_rule_is_refused(self):
        bad = [
            {"hysteresis_c": -0.1},
            {"min_on_minutes": -1},
            {"min_off_minutes": float("nan")},
            {"start_every_minutes": float("inf")},
            {"high_margin_c": -2},
            {"start_window_minutes": 0},
            {"start_window_minutes": 31},
            {"start_every_minutes": 5},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaisesRegex(hp.ConfigError, "call:"):
                hp.parse_config(dict(ARGS, call=raw))
        hp.parse_config(dict(ARGS, call={"start_window_minutes": 30}))
        hp.parse_config(dict(ARGS, call={"hysteresis_c": 0, "min_on_minutes": 0}))

    def test_the_window_lockout_has_defaults_and_is_read(self):
        w = CFG.window
        self.assertEqual((w.lockout_per_open_hour, w.lockout_max_hours), (1.0, 4.0))
        self.assertEqual((w.settle_max_minutes, w.settle_recovered_c), (60.0, 0.3))
        cfg = hp.parse_config(
            dict(ARGS, window={"lockout_per_open_hour": 1.5, "lockout_max_hours": 6})
        )
        self.assertEqual((cfg.window.lockout_per_open_hour, cfg.window.lockout_max_hours), (1.5, 6.0))
        self.assertEqual(cfg.window.settle_max_minutes, 60.0)

    def test_a_bad_window_setting_is_refused(self):
        bad = [
            {"lockout_per_open_hour": -1},
            {"lockout_max_hours": -1},
            {"settle_max_minutes": float("nan")},
            {"open_debounce_minutes": -2},
            {"lockout_max_hours": 0.5},
            {"settle_max_minutes": 600, "lockout_max_hours": 8},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaisesRegex(hp.ConfigError, "window:"):
                hp.parse_config(dict(ARGS, window=raw))
        hp.parse_config(dict(ARGS, window={"lockout_per_open_hour": 0, "lockout_max_hours": 1}))

    def test_the_settings_of_the_old_model_are_ignored(self):
        old = {
            "trim": {"initial": 0.5, "min": -0.5, "max": 1.5},
            "weather_offset": {"gain": 0.85, "bias": 0.22, "max_c": 1.5},
            "warm_hold": {"enter_c": 1.0, "release_c": 0.6},
        }
        cfg = hp.parse_config(dict(self._broken(**old), window={"long_open_minutes": 30}))
        self.assertEqual(cfg.zones["family"].base_c, 21.5)
        self.assertEqual(cfg.window, hp.WindowConfig())

    def test_bad_signal_and_alert_settings_are_refused(self):
        zones = lambda **kw: dict(ARGS["zones"], kristines_room=dict(KRISTINE, **kw))  # noqa: E731
        cases = [
            dict(ARGS, alerts={"calling_hours": 0}),
            dict(ARGS, alerts={"evenness_nights": 0}),
            dict(ARGS, alerts={"cold_minutes": -1}),
            dict(ARGS, alerts={"repeat_hours": -1}),
            dict(ARGS, zones=zones(buttons={"warmer": "a", "hotter": "b"})),
            dict(ARGS, zones=zones(evenness_floors=["only.one"])),
            dict(ARGS, zones=zones(evenness_floors=["a", "b", "c"])),
        ]
        for args in cases:
            with self.assertRaises(hp.ConfigError, msg=repr(args)[:80]):
                hp.parse_config(args)

    def test_floor_max_is_optional_and_may_exceed_the_upper_setpoint_limit(self):
        self.assertIsNone(CFG.zones["family"].floor_max_c)
        for value in (19.1, 24, 26.0):
            with self.subTest(value=value):
                zc = hp.parse_config(self._broken(floor_max_c=value)).zones["family"]
                self.assertEqual(zc.floor_max_c, value)
                self.assertIsInstance(zc.floor_max_c, float)

    def test_invalid_floor_limits_are_refused(self):
        invalid = (
            19.0, 18.0, True, False, "26.0", "bad", [], {},
            float("nan"), float("inf"), -float("inf"),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(
                hp.ConfigError, "zone family: floor_max_c"
            ):
                hp.parse_config(self._broken(floor_max_c=value))

    WEIGHTS = {
        "sensor.living_room_feel": 19,
        "sensor.dining_room_feel": 18.5,
        "sensor.kitchen_feel": 18.5,
    }

    def weights_of(self, raw):
        return hp.parse_config(self._broken(control_air_weights=raw)).zones["family"]

    def test_control_air_weights_are_read_in_the_order_of_control_air(self):
        reordered = {e: self.WEIGHTS[e] for e in reversed(list(self.WEIGHTS))}
        for raw in (self.WEIGHTS, reordered):
            self.assertEqual(self.weights_of(raw).control_air_weights, (19.0, 18.5, 18.5))
        zc = self.weights_of({e: 1 for e in FAMILY["control_air"]})
        self.assertEqual(zc.control_air_weights, (1.0, 1.0, 1.0))

    def test_control_air_weights_are_optional_and_leave_the_other_zones_equal(self):
        for raw in (None, {}):
            self.assertEqual(self.weights_of(raw).control_air_weights, ())
        self.assertEqual({zc.control_air_weights for zc in CFG.zones.values()}, {()})

    def test_bad_control_air_weights_are_refused(self):
        living, dining, kitchen = FAMILY["control_air"]
        bad = (
            [19, 18.5, 18.5],
            "equal",
            {living: 19, dining: 18.5},
            dict(self.WEIGHTS, **{"sensor.hall_feel": 5}),
            {living: 19, dining: 18.5, "sensor.kitchn_feel": 18.5},
            dict(self.WEIGHTS, **{kitchen: 0}),
            dict(self.WEIGHTS, **{kitchen: -1}),
            dict(self.WEIGHTS, **{kitchen: True}),
            dict(self.WEIGHTS, **{kitchen: "18.5"}),
            dict(self.WEIGHTS, **{kitchen: None}),
            dict(self.WEIGHTS, **{kitchen: float("nan")}),
            dict(self.WEIGHTS, **{kitchen: float("inf")}),
        )
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaisesRegex(
                hp.ConfigError, "zone family: control_air_weights"
            ):
                self.weights_of(raw)

    def floors_of(self, raw):
        return hp.parse_config(self._broken(floor=raw)).zones["family"].floor_sensors

    def test_floor_is_one_sensor_or_a_list_of_them(self):
        self.assertEqual(self.floors_of("sensor.a"), ("sensor.a",))
        self.assertEqual(self.floors_of(["sensor.a"]), ("sensor.a",))
        self.assertEqual(self.floors_of(["sensor.a", "sensor.b"]), ("sensor.a", "sensor.b"))
        self.assertEqual(self.floors_of(["sensor.a", "sensor.a"]), ("sensor.a",))
        for none in (None, "", []):
            self.assertEqual(self.floors_of(none), ())
        self.assertEqual(CFG.zones["family"].floor_sensors, (FAMILY["floor"],))
        self.assertEqual(CFG.zones["bathroom"].floor_sensors, ())

    def test_a_floor_that_is_not_a_sensor_name_is_refused(self):
        for raw in (5, [5], ["sensor.a", None], ["sensor.a", ""], [["sensor.a"]], {"a": "b"}):
            with self.subTest(raw=raw), self.assertRaisesRegex(
                hp.ConfigError, "zone family: floor"
            ):
                self.floors_of(raw)

    ADVISED = {
        "family": {
            "rooms": ["sensor.living_room_feel", "sensor.dining_room_feel", "sensor.kitchen_feel"]
        },
        "kristines_room": {"rooms": ["sensor.kristines_room_feel"], "margin_c": 0.2},
        "claudias_room": {"rooms": "sensor.claudias_room_feel"},
    }

    def advisor(self, **kw):
        return dict(ARGS, season_advisor=dict({"zones": self.ADVISED}, **kw))

    def test_the_season_advisor_is_off_unless_the_config_has_one(self):
        self.assertIsNone(CFG.season_advisor)
        self.assertIsNone(hp.parse_config(dict(ARGS, season_advisor={})).season_advisor)

    def test_the_season_advisor_has_defaults(self):
        a = hp.parse_config(self.advisor()).season_advisor
        self.assertEqual(a.zones, ("family", "kristines_room", "claudias_room"))
        self.assertEqual(
            a.rooms,
            {
                "family": (
                    "sensor.living_room_feel",
                    "sensor.dining_room_feel",
                    "sensor.kitchen_feel",
                ),
                "kristines_room": ("sensor.kristines_room_feel",),
                "claudias_room": ("sensor.claudias_room_feel",),
            },
        )
        self.assertEqual(a.margins, {"kristines_room": 0.2})
        self.assertEqual((a.margin("family"), a.margin("kristines_room")), (0.5, 0.2))
        self.assertEqual(
            (a.evaluate_at, a.margin_c, a.mornings, a.start_below_c), (480, 0.5, 2, 12.0)
        )
        self.assertEqual((a.stop_from, a.stop_until), ((3, 1), (9, 30)))
        self.assertEqual((a.look_back_days, a.idle_days, a.idle_hours), (7, 5, 2.0))
        self.assertEqual((a.stop_min_c, a.season_min_days, a.reask_days), (5.0, 7, 3))

    def test_the_season_advisor_settings_are_read(self):
        cfg = hp.parse_config(
            self.advisor(
                evaluate_at="06:30",
                margin_c=1,
                mornings=3,
                start_below_c=8,
                stop_from="02-15",
                stop_until="10-31",
                look_back_days=10,
                idle_days=6,
                idle_hours=1.5,
                stop_min_c=6,
                season_min_days=3,
                reask_days=2,
            )
        )
        a = cfg.season_advisor
        self.assertEqual(
            (a.evaluate_at, a.margin_c, a.mornings, a.start_below_c), (390, 1.0, 3, 8.0)
        )
        self.assertEqual((a.stop_from, a.stop_until), ((2, 15), (10, 31)))
        self.assertEqual((a.look_back_days, a.idle_days, a.idle_hours), (10, 6, 1.5))
        self.assertEqual((a.stop_min_c, a.season_min_days, a.reask_days), (6.0, 3, 2))
        self.assertEqual((a.margin("family"), a.margin("kristines_room")), (1.0, 0.2))

    def test_the_zones_keep_the_order_the_config_gives_and_repeat_no_room(self):
        zones = {
            "claudias_room": {"rooms": ["sensor.a", "sensor.a", "sensor.b"]},
            "family": {"rooms": ["sensor.c"]},
        }
        a = hp.parse_config(dict(ARGS, season_advisor={"zones": zones})).season_advisor
        self.assertEqual(a.zones, ("claudias_room", "family"))
        self.assertEqual(a.rooms["claudias_room"], ("sensor.a", "sensor.b"))

    def test_bad_season_advisor_settings_are_refused(self):
        def zones(**kw):
            return dict(ARGS, season_advisor={"zones": kw})

        cases = [
            dict(ARGS, season_advisor={"evaluate_at": "08:00"}),
            dict(ARGS, season_advisor={"zones": ["family", "kristines_room"]}),
            zones(),
            zones(nowhere={"rooms": ["sensor.a"]}),
            zones(family=None),
            zones(family={}),
            zones(family={"rooms": []}),
            zones(family={"rooms": [None]}),
            zones(family={"rooms": [5]}),
            zones(family={"rooms": ["sensor.a"], "margin_c": -0.1}),
            self.advisor(evaluate_at="05:59"),
            self.advisor(evaluate_at="0800"),
            self.advisor(evaluate_at=800),
            self.advisor(margin_c=-0.1),
            self.advisor(idle_hours=0),
            self.advisor(season_min_days=-1),
            self.advisor(mornings=0),
            self.advisor(look_back_days=0),
            self.advisor(idle_days=0),
            self.advisor(reask_days=0),
            self.advisor(look_back_days=5, idle_days=6),
            self.advisor(stop_from="10-01", stop_until="03-01"),
            self.advisor(stop_from="02-30"),
            self.advisor(stop_until="0930"),
        ]
        for args in cases:
            with self.assertRaises(hp.ConfigError, msg=repr(args)[:120]):
                hp.parse_config(args)
        hp.parse_config(self.advisor(evaluate_at="06:00", look_back_days=5, idle_days=5))
        hp.parse_config(self.advisor(stop_from="03-01", stop_until="03-01", season_min_days=0))

    TRANSIT = {"loops": {"zone_1": 2.0, "zone_4": 1.5}, "tau_minutes": 40, "k_per_kwh": 0.2}
    PLANT = {"supply_temp_entity": "sensor.supply", "relay_power_entity": "sensor.relay"}

    def transit_of(self, transit, **plant):
        args = dict(self._broken(transit=transit), **(plant or self.PLANT))
        return hp.parse_config(args).zones["family"].transit

    def test_a_zone_has_no_transit_unless_the_config_gives_one(self):
        self.assertEqual({zc.transit for zc in CFG.zones.values()}, {None})
        for empty in (None, {}):
            self.assertIsNone(self.transit_of(empty))

    def test_a_transit_block_is_read_with_the_probe_of_each_loop(self):
        transit = self.transit_of(self.TRANSIT)
        self.assertEqual(
            transit.loops,
            (
                hp.TransitLoop("zone_1", "sensor.heating_room_zone_1_return", 2.0),
                hp.TransitLoop("zone_4", "sensor.heating_room_zone_4_return", 1.5),
            ),
        )
        self.assertEqual((transit.tau_minutes, transit.k_per_kwh), (40.0, 0.2))
        self.assertIsInstance(transit.tau_minutes, float)

    def test_a_bad_transit_block_is_refused(self):
        bad = [
            {"loops": {}},
            {"loops": None},
            {"loops": ["zone_1"]},
            {"loops": {"zone_1": 0}},
            {"loops": {"zone_1": -1.0}},
            {"loops": {"zone_1": True}},
            {"loops": {"zone_1": "2"}},
            {"loops": {"zone_1": float("nan")}},
            {"loops": {"zone_1": float("inf")}},
            {"loops": {1: 2.0}},
            {"loops": {"": 2.0}},
            {"tau_minutes": 0},
            {"tau_minutes": None},
            {"tau_minutes": float("inf")},
            {"k_per_kwh": 0},
            {"k_per_kwh": -0.2},
            {"k_per_kwh": "0.2"},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaisesRegex(
                hp.ConfigError, "zone family: transit"
            ):
                self.transit_of(dict(self.TRANSIT, **change))
        for missing in ("loops", "tau_minutes", "k_per_kwh"):
            with self.subTest(missing=missing), self.assertRaisesRegex(
                hp.ConfigError, "zone family: transit"
            ):
                self.transit_of({k: v for k, v in self.TRANSIT.items() if k != missing})

    def test_a_transit_block_needs_the_supply_probe_and_the_pump_power(self):
        plants = (
            {},
            {"supply_temp_entity": "sensor.supply"},
            {"relay_power_entity": "sensor.relay"},
        )
        for plant in plants:
            with self.subTest(plant=plant), self.assertRaisesRegex(
                hp.ConfigError, "transit needs supply_temp_entity and relay_power_entity"
            ):
                hp.parse_config(dict(self._broken(transit=self.TRANSIT), **plant))

    def when_home_of(self, value):
        profile = {"lead_hours": 4, "when_home": value}
        return hp.parse_config(self._broken(profile=profile)).zones["family"].profile.when_home

    def test_only_a_profile_that_lists_when_home_entities_waits_for_anyone(self):
        waiting = {n: zc.profile.when_home for n, zc in CFG.zones.items() if zc.profile.when_home}
        self.assertEqual(waiting, {"bathroom": ("person.mikkel", "device_tracker.of9p")})

    def test_when_home_is_an_entity_or_a_list_of_entities(self):
        self.assertEqual(self.when_home_of("person.a"), ("person.a",))
        self.assertEqual(
            self.when_home_of(["person.a", "device_tracker.b"]), ("person.a", "device_tracker.b")
        )
        for empty in (None, "", []):
            self.assertEqual(self.when_home_of(empty), ())

    def test_a_bad_when_home_is_refused(self):
        for bad in ([1], [None], [""], [["person.a"]], {"person.a": "home"}, 5, True):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                hp.ConfigError, "zone family: profile when_home"
            ):
                self.when_home_of(bad)

    def travel_of(self, value, when_home=("person.a",)):
        profile = {"lead_hours": 4, "when_home": list(when_home), "travel": value}
        return hp.parse_config(self._broken(profile=profile)).zones["family"].profile.travel

    def test_a_travel_block_names_its_tracker_distance_and_lower_limit(self):
        travel = {"tracker": "device_tracker.p", "km": 50, "lower_limit_c": 17}
        self.assertEqual(self.travel_of(travel), hp.Travel("device_tracker.p", 50.0, 17.0))
        self.assertIsNone(CFG.zones["family"].profile.travel)
        bad = [
            ({"km": 50, "lower_limit_c": 17}, ("person.a",)),
            (travel, ()),
            ({**travel, "km": 0}, ("person.a",)),
            ({**travel, "lower_limit_c": 4}, ("person.a",)),
            ("device_tracker.p", ("person.a",)),
        ]
        for value, when_home in bad:
            with self.subTest(value=value), self.assertRaisesRegex(
                hp.ConfigError, "zone family: profile travel"
            ):
                self.travel_of(value, when_home)

    def test_distance_is_the_great_circle_in_km(self):
        self.assertAlmostEqual(hp.distance_km((55.6761, 12.5683), (56.1629, 10.2039)), 156.9, 0)
        self.assertEqual(hp.distance_km((55.0, 12.0), (55.0, 12.0)), 0.0)

    def _broken(self, **zone_change):
        zones = dict(ARGS["zones"], family=dict(FAMILY, **zone_change))
        return dict(ARGS, zones=zones)

    def test_errors_are_explicit(self):
        cases = [
            dict(ARGS, zones={}),
            dict(ARGS, season_entity=""),
            self._broken(thermostat=""),
            self._broken(lower_limit_c=25.0),
            self._broken(control_air=[]),
            self._broken(protected_min_offset_c=-0.5),
            self._broken(profile={"periods": [{"from": "10:00", "to": "10:00"}]}),
            self._broken(profile={"periods": [{"from": "1000", "to": "11:00"}]}),
            self._broken(profile={"outside": "nonsense"}),
            self._broken(sun_cut={"enabled": True, "azimuth": [200, 100]}),
            self._broken(bedroom_guard={"enabled": True, "cap": "nonsense"}),
            dict(ARGS, party={"zone": "nowhere"}),
            dict(ARGS, bedroom={}),  # the bathroom guard needs a ceiling entity
            dict(
                ARGS,
                bedroom={"ceiling_entity": "x", "evening_from": "23:30", "evening_to": "21:00"},
            ),
        ]
        for args in cases:
            with self.assertRaises(hp.ConfigError, msg=repr(args)[:80]):
                hp.parse_config(args)


class ComfortTarget(unittest.TestCase):
    zc = CFG.zones["family"]

    def target(self, hour, minute=0, nudge=0.0):
        return hp.comfort_target(self.zc, 21.5, nudge, T(hour, minute), TZ, 19.0)

    def test_profile_bands(self):
        self.assertEqual(self.target(12), 21.5)
        self.assertEqual(self.target(17), 22.0)
        self.assertEqual(self.target(22, 59), 22.0)
        self.assertEqual(self.target(23), 21.0)
        self.assertEqual(self.target(9, 59), 21.0)
        self.assertEqual(self.target(10), 21.5)

    def test_night_band_is_continuous_across_midnight(self):
        self.assertEqual(self.target(23, 30), 21.0)
        self.assertEqual(self.target(0, 30), 21.0)
        self.assertEqual(self.target(3), 21.0)

    def test_nudge_moves_every_band(self):
        self.assertEqual(self.target(12, nudge=0.5), 22.0)
        self.assertEqual(self.target(20, nudge=-1.0), 21.0)

    def test_bathroom_is_at_the_lower_limit_outside_its_morning_window(self):
        zc = CFG.zones["bathroom"]
        at = lambda h, m=0, base=22.0: hp.comfort_target(
            zc, base, 0.0, T(h, m), TZ, 19.0
        )  # noqa: E731
        self.assertEqual(at(5, 59), 19.0)
        self.assertEqual(at(6), 22.0)
        self.assertEqual(at(8, 59), 22.0)
        self.assertEqual(at(9), 19.0)
        # moving the base moves the morning level only
        self.assertEqual(at(7, base=23.0), 23.0)
        self.assertEqual(at(12, base=23.0), 19.0)

    def test_a_user_who_is_away_is_outside_every_period(self):
        bath = CFG.zones["bathroom"]
        self.assertEqual(hp.comfort_target(bath, 22.0, 0.0, T(7), TZ, 19.0), 22.0)
        self.assertEqual(hp.comfort_target(bath, 22.0, 0.0, T(7), TZ, 19.0, away=True), 19.0)
        self.assertEqual(self.target(23, nudge=0.5), 21.5)
        self.assertEqual(hp.comfort_target(self.zc, 21.5, 0.5, T(23), TZ, 19.0, away=True), 22.0)

    def test_protected_minimum(self):
        fam, kri, bath = (CFG.zones[z] for z in ("family", "kristines_room", "bathroom"))
        self.assertEqual(hp.protected_min(fam, 21.5, 0.0, 19.0), 20.5)
        self.assertEqual(hp.protected_min(kri, 21.5, 0.5, 19.0), 21.5)
        self.assertEqual(hp.protected_min(bath, 22.0, 0.0, 19.0), 19.0)
        # never below the lower limit
        self.assertEqual(hp.protected_min(kri, 19.2, 0.0, 19.0), 19.0)


class RoundingHelpers(unittest.TestCase):
    def test_round_step_takes_the_nearest_device_step_half_up(self):
        self.assertEqual(hp.round_step(21.24), 21.0)
        self.assertEqual(hp.round_step(21.25), 21.5)
        self.assertEqual(hp.round_step(21.74), 21.5)
        self.assertEqual(hp.round_step(21.75), 22.0)
        self.assertEqual(hp.round_step(22.0), 22.0)

    def test_ceil_step_takes_the_step_at_or_above(self):
        self.assertEqual(hp.ceil_step(23.5), 23.5)
        self.assertEqual(hp.ceil_step(23.51), 24.0)
        self.assertEqual(hp.ceil_step(23.0), 23.0)
        self.assertEqual(hp.ceil_step(22.01), 22.5)
        self.assertEqual(hp.ceil_step(19.1 + 2.0), 21.5)

    def test_ceil_step_is_not_pushed_up_by_float_noise(self):
        self.assertEqual(hp.ceil_step(21.1 + 2.4), 23.5)
        self.assertEqual(hp.ceil_step(0.1 + 0.2 + 22.2), 22.5)
        self.assertEqual(hp.ceil_step(20.3 + 2.2), 22.5)

    def test_clamp(self):
        self.assertEqual(hp.clamp(25.5, 19.0, 24.0), 24.0)
        self.assertEqual(hp.clamp(10.0, 19.0, 24.0), 19.0)
        self.assertEqual(hp.clamp(21.0, 19.0, 24.0), 21.0)


class AimByTime(unittest.TestCase):
    """The family day: lead 4 h, so the aim leads the profile by four hours."""

    def aim(self, hour, minute=0, **kw):
        return plan_at(T(hour, minute), **kw)

    def test_hold_follows_the_shifted_profile(self):
        expected = {(0, 0): 21.0, (5, 59): 21.0, (6, 0): 21.5, (12, 59): 21.5, (13, 0): 22.0}
        expected.update({(18, 59): 22.0, (19, 0): 21.0, (23, 59): 21.0})
        for (h, m), want in expected.items():
            p = self.aim(h, m)
            self.assertEqual((p.mode, p.allowed, p.aim), ("hold", True, want), (h, m))

    def test_the_decided_target_numbers(self):
        self.assertEqual(self.aim(12).target, 21.5)
        self.assertEqual(self.aim(18).target, 22.0)
        self.assertEqual(self.aim(2).target, 21.0)

    def test_resident_room_is_flat_and_moves_with_the_nudge(self):
        for h in (3, 12, 20):
            self.assertEqual(plan_at(T(h), "kristines_room", nudge=0.0).aim, 21.5)
            self.assertEqual(plan_at(T(h), "kristines_room", nudge=-1.0).aim, 20.5)
        self.assertEqual(plan_at(T(12), "claudias_room", nudge=1.0).aim, 22.5)

    def test_guest_bathroom_is_flat_at_its_own_level(self):
        for h in (3, 12, 20):
            self.assertEqual(plan_at(T(h), "guest_bathroom").aim, 20.0)

    def test_bathroom_has_no_overnight_block(self):
        for minutes in range(0, 24 * 60, 5):
            p = plan_at(T(minutes // 60, minutes % 60), "bathroom")
            if 6 * 60 <= minutes < 9 * 60:
                self.assertEqual(p.aim, 22.0, minutes)
            else:
                self.assertEqual(p.aim, 19.0, minutes)

    def test_the_aim_is_clamped_to_the_limits(self):
        self.assertEqual(plan_at(T(12), base=25.5).aim, 24.0)
        self.assertEqual(plan_at(T(12), base=18.0).aim, 19.0)
        self.assertEqual(plan_at(T(12), base=23.5).aim, 23.5)

    def test_effective_limits_can_be_tighter_than_the_configured_ones(self):
        self.assertEqual(plan_at(T(12), lower_c=21.0, upper_c=21.0).aim, 21.0)
        self.assertEqual(plan_at(T(12), lower_c=21.8, upper_c=22.5).aim, 21.8)
        self.assertEqual(plan_at(T(8, 0), "bathroom", lower_c=19.5).aim, 22.0)
        self.assertEqual(plan_at(T(2, 0), "bathroom", lower_c=19.5).aim, 19.5)

    def test_the_aim_has_no_trim_and_no_rounding_to_the_device_step(self):
        self.assertEqual(plan_at(T(12), base=21.7).aim, 21.7)
        self.assertEqual(plan_at(T(12), base=21.3, nudge=0.25).aim, 21.55)

    def test_the_plan_does_not_depend_on_what_was_written_before(self):
        self.assertEqual(plan_at(T(12)), plan_at(T(12)))

    def test_the_plan_decides_only_whether_heat_is_allowed_and_the_aim(self):
        self.assertEqual(
            list(hp.Plan.__dataclass_fields__),
            ["mode", "plan_mode", "target", "allowed", "aim", "protected_min", "reasons"],
        )


class AllowedByMode(unittest.TestCase):
    """Heat is allowed in the aim modes and not allowed in the gate modes."""

    CUT = ((T(1), T(14), "2026-10-17"),)
    PARTY = ((T(8), T(18)),)

    def test_the_aim_modes_allow_heat(self):
        cases = [
            ("hold", "family", T(12), {}),
            ("sun_cut", "family", T(4), dict(cut_windows=self.CUT)),
            ("fallback", "family", T(12), dict(fallback=True)),
            ("guard", "bathroom", T(7), dict(hold_back=True)),
        ]
        for mode, zone, now, kw in cases:
            p = plan_at(now, zone, **kw)
            self.assertEqual((p.mode, p.allowed), (mode, True), mode)

    def test_the_gate_modes_do_not_allow_heat_and_keep_the_aim(self):
        cases = [
            ("window", dict(window_state=hp.STATE_OPEN)),
            ("settle", dict(window_state=hp.STATE_SETTLE)),
            ("party", dict(party_windows=self.PARTY)),
            ("floor_cap", dict(floor_cap=True)),
        ]
        zone = replace(CFG.zones["family"], floor_max_c=26.0)
        for mode, kw in cases:
            p = hp.plan(zone, inputs(**kw), T(12), TZ)
            self.assertEqual((p.mode, p.allowed, p.aim), (mode, False, 21.5), mode)

    def test_a_gate_mode_still_computes_the_aim_of_the_modes_below_it(self):
        p = plan_at(T(4), window_state=hp.STATE_OPEN, cut_windows=self.CUT)
        self.assertEqual((p.mode, p.allowed, p.aim), ("window", False, 20.5))
        p = plan_at(T(7), "bathroom", window_state=hp.STATE_SETTLE, hold_back=True)
        self.assertEqual((p.mode, p.allowed, p.aim), ("settle", False, 19.0))

    def test_the_season_and_the_device_do_not_change_what_the_plan_allows(self):
        for kw, mode in ((dict(season_on=False), "off_season"), (dict(online=False), "device_offline")):
            p = plan_at(T(12), **kw)
            self.assertEqual((p.mode, p.plan_mode, p.allowed, p.aim), (mode, "hold", True, 21.5))
        p = plan_at(T(12), season_on=False, window_state=hp.STATE_OPEN)
        self.assertEqual((p.mode, p.plan_mode, p.allowed), ("off_season", "window", False))


class ModePrecedence(unittest.TestCase):
    """window > settle > party > floor_cap > guard > sun_cut > fallback > travel > away > hold."""

    zone = replace(
        CFG.zones["family"],
        guard=hp.GuardConfig(True, "lower_limit"),
        floor_max_c=26.0,
    )
    waiting = replace(zone, profile=replace(zone.profile, when_home=("person.mikkel",)))
    travelling = replace(
        waiting, profile=replace(waiting.profile, travel=hp.Travel("device_tracker.p", 50.0, 17.0))
    )

    def mode_of(self, **kw):
        base = dict(
            fallback=True,
            hold_back=True,
            floor_cap=True,
            party_windows=((T(8), T(18)),),
            cut_windows=((T(1), T(14), "2026-10-17"),),
        )
        base.update(kw)
        p = hp.plan(self.zone, inputs(**base), T(12), TZ)
        return p.plan_mode

    def test_each_rung_wins_over_the_ones_below_it(self):
        self.assertEqual(self.mode_of(window_state=hp.STATE_OPEN), "window")
        self.assertEqual(self.mode_of(window_state=hp.STATE_SETTLE), "settle")
        self.assertEqual(self.mode_of(), "party")
        self.assertEqual(self.mode_of(party_windows=()), "floor_cap")
        self.assertEqual(self.mode_of(party_windows=(), floor_cap=False), "guard")
        self.assertEqual(
            self.mode_of(party_windows=(), floor_cap=False, hold_back=False), "fallback"
        )
        self.assertEqual(
            self.mode_of(party_windows=(), floor_cap=False, hold_back=False, fallback=False),
            "sun_cut",
        )
        no_cut = dict(
            party_windows=(), floor_cap=False, hold_back=False, fallback=False, cut_windows=()
        )
        self.assertEqual(self.mode_of(**no_cut), "hold")

    def test_away_wins_over_hold_and_nothing_else(self):
        def mode_of(**kw):
            base = dict(away=True, fallback=False, hold_back=False, floor_cap=False)
            base.update(kw)
            return hp.plan(self.waiting, inputs(**base), T(12), TZ).plan_mode

        self.assertEqual(mode_of(), "away")
        self.assertEqual(mode_of(away=False), "hold")
        self.assertEqual(mode_of(fallback=True), "fallback")
        self.assertEqual(mode_of(cut_windows=((T(1), T(14), "2026-10-17"),)), "sun_cut")
        self.assertEqual(mode_of(hold_back=True), "guard")
        self.assertEqual(mode_of(floor_cap=True), "floor_cap")
        self.assertEqual(mode_of(party_windows=((T(8), T(18)),)), "party")
        self.assertEqual(mode_of(window_state=hp.STATE_SETTLE), "settle")
        self.assertEqual(mode_of(window_state=hp.STATE_OPEN), "window")

    def test_travel_holds_the_lower_limit_given_and_wins_over_away(self):
        def plan(**kw):
            base = dict(away=True, travelling=True, fallback=False, hold_back=False)
            base.update(floor_cap=False, lower_c=17.0, **kw)
            return hp.plan(self.travelling, inputs(**base), T(12), TZ)

        p = plan()
        self.assertEqual((p.plan_mode, p.aim, p.reasons), (
            "travel", 17.0, ("Mikkel travelling: holding 17.0 °C",)
        ))
        self.assertEqual(plan(travelling=False).plan_mode, "away")
        self.assertEqual(plan(away=False).plan_mode, "hold")
        self.assertEqual(plan(fallback=True).plan_mode, "fallback")
        self.assertEqual(plan(window_state=hp.STATE_OPEN).plan_mode, "window")
        without = hp.plan(self.waiting, inputs(away=True, travelling=True, fallback=False,
                                               hold_back=False, floor_cap=False), T(12), TZ)
        self.assertEqual(without.plan_mode, "away")

    def test_fallback_suppresses_cuts_but_not_window_or_party(self):
        zc = CFG.zones["family"]
        cut = ((T(1), T(14), "2026-10-17"),)
        self.assertEqual(
            hp.plan(zc, inputs(fallback=True, cut_windows=cut), T(12), TZ).plan_mode, "fallback"
        )
        self.assertEqual(
            hp.plan(zc, inputs(fallback=True, window_state="open"), T(12), TZ).plan_mode, "window"
        )
        self.assertEqual(
            hp.plan(zc, inputs(fallback=True, party_windows=((T(8), T(18)),)), T(12), TZ).plan_mode,
            "party",
        )

    def test_a_cut_window_that_is_not_running_changes_nothing(self):
        zc = CFG.zones["family"]
        p = hp.plan(zc, inputs(cut_windows=((T(1), T(11), "2026-10-17"),)), T(12), TZ)
        self.assertEqual(p.plan_mode, "hold")

    def test_the_season_and_device_gate_hide_the_mode_but_not_the_plan(self):
        p = plan_at(T(12), season_on=False)
        self.assertEqual((p.mode, p.plan_mode, p.aim), ("off_season", "hold", 21.5))
        p = plan_at(T(12), online=False)
        self.assertEqual((p.mode, p.plan_mode), ("device_offline", "hold"))
        p = plan_at(T(12), season_on=False, online=False)
        self.assertEqual(p.mode, "off_season")
        self.assertIn("Heating season is off", p.reasons[0])

    def test_window_party_and_settle_keep_heat_off_and_the_aim_in_view(self):
        for kw in (
            dict(window_state="open"),
            dict(window_state="settle"),
            dict(party_windows=((T(8), T(18)),)),
        ):
            p = plan_at(T(12), **kw)
            self.assertEqual((p.allowed, p.aim), (False, 21.5), kw)

    def test_sun_cut_aims_at_the_protected_minimum(self):
        cut = ((T(1), T(14), "2026-10-17"),)
        p = plan_at(T(4), cut_windows=cut)
        self.assertEqual((p.mode, p.allowed, p.aim), ("sun_cut", True, 20.5))
        # the cut never raises the plan: an aim below the protected minimum stays where it is
        p = plan_at(T(4), cut_windows=cut, base=19.5)
        self.assertEqual((p.target, p.aim), (19.0, 19.0))

    def test_cut_is_ignored_for_a_zone_without_sun_cut(self):
        cut = ((T(1), T(14), "2026-10-17"),)
        self.assertEqual(plan_at(T(4), "kristines_room", cut_windows=cut).mode, "hold")

    def test_fallback_is_a_constant_without_profile(self):
        p = plan_at(T(13), fallback=True)  # hold would aim at 22.0 here
        self.assertEqual((p.mode, p.aim), ("fallback", 21.5))
        p = plan_at(T(20), fallback=True, nudge=0.5)
        self.assertEqual(p.aim, 22.0)

    def test_bathroom_fallback_stays_at_the_lower_limit(self):
        p = plan_at(T(3), "bathroom", fallback=True)
        self.assertEqual((p.mode, p.aim), ("fallback", 19.0))

    def test_bedroom_hold_back_aims_the_bathroom_at_the_lower_limit(self):
        p = plan_at(T(7), "bathroom", hold_back=True)
        self.assertEqual((p.mode, p.allowed, p.aim), ("guard", True, 19.0))
        p = plan_at(T(7), "bathroom", hold_back=False)
        self.assertEqual((p.mode, p.allowed, p.aim), ("hold", True, 22.0))

    def test_hold_back_does_not_touch_the_family_zone(self):
        p = plan_at(T(4), "family", hold_back=True)
        self.assertEqual(p.mode, "hold")
        self.assertEqual(p.aim, 21.0)

    def test_window_in_the_bathroom_beats_the_hold_back_and_the_morning(self):
        self.assertEqual(plan_at(T(7), "bathroom", window_state="open").mode, "window")


class UserAway(unittest.TestCase):
    profile = CFG.zones["bathroom"].profile
    PERSON, PHONE = profile.when_home

    def away(self, person, phone):
        return hp.user_away(self.profile, {self.PERSON: person, self.PHONE: phone})

    def test_both_entities_elsewhere_is_away_at_not_home_or_in_a_zone(self):
        self.assertTrue(self.away("not_home", "not_home"))
        self.assertTrue(self.away("not_home", "Pernille"))
        self.assertTrue(self.away("Office", "Pernille"))

    def test_either_entity_at_home_is_home(self):
        self.assertFalse(self.away("home", "home"))
        self.assertFalse(self.away("home", "not_home"))
        self.assertFalse(self.away("not_home", "home"))
        self.assertFalse(self.away("Pernille", "home"))

    def test_an_entity_that_cannot_be_read_counts_as_home(self):
        for unreadable in ("unknown", "unavailable", "", None):
            with self.subTest(unreadable=unreadable):
                self.assertFalse(self.away("not_home", unreadable))
                self.assertFalse(self.away(unreadable, "Pernille"))
                self.assertFalse(self.away(unreadable, unreadable))

    def test_a_missing_entity_counts_as_home(self):
        self.assertFalse(hp.user_away(self.profile, {self.PERSON: "not_home"}))
        self.assertFalse(hp.user_away(self.profile, {self.PHONE: "not_home"}))
        self.assertFalse(hp.user_away(self.profile, {}))

    def test_only_the_listed_entities_count(self):
        states = {self.PERSON: "not_home", self.PHONE: "not_home", "person.claudia": "home"}
        self.assertTrue(hp.user_away(self.profile, states))

    def test_a_profile_that_waits_for_nobody_is_never_away(self):
        family = CFG.zones["family"].profile
        self.assertFalse(hp.user_away(family, {}))
        self.assertFalse(hp.user_away(family, {self.PERSON: "not_home", self.PHONE: "not_home"}))

    def test_the_reason_names_the_first_person_entity(self):
        self.assertEqual(hp.home_label(self.profile), "Mikkel")
        named = replace(self.profile, when_home=("device_tracker.x", "person.mikkel_eskildsen"))
        self.assertEqual(hp.home_label(named), "Mikkel Eskildsen")
        nameless = replace(self.profile, when_home=("device_tracker.of9p",))
        self.assertEqual(hp.home_label(nameless), "User")


class AwayFromHome(unittest.TestCase):
    """A profile that waits for its user runs its periods only while the user is home."""

    def test_at_home_the_period_applies(self):
        p = plan_at(T(7), "bathroom")
        self.assertEqual((p.mode, p.allowed, p.target, p.aim), ("hold", True, 22.0, 22.0))

    def test_away_the_bathroom_holds_its_lower_limit_around_the_clock(self):
        for minutes in range(0, 24 * 60, 5):
            p = plan_at(T(minutes // 60, minutes % 60), "bathroom", away=True)
            self.assertEqual((p.mode, p.allowed, p.target, p.aim), ("away", True, 19.0, 19.0))
            self.assertEqual(p.reasons, ("Mikkel away: holding 19.0 °C",))

    def test_the_limits_still_bound_the_aim_while_away(self):
        self.assertEqual(plan_at(T(7), "bathroom", away=True, lower_c=19.5).aim, 19.5)

    def test_coming_home_mid_period_applies_it_from_that_moment(self):
        sweep = [(6, 0), (6, 55), (7, 0), (7, 5)]
        aims = [plan_at(T(h, m), "bathroom", away=(h, m) < (7, 0)).aim for h, m in sweep]
        self.assertEqual(aims, [19.0, 19.0, 22.0, 22.0])

    def test_leaving_mid_period_takes_the_rest_of_it_away(self):
        sweep = [(6, 0), (7, 0), (7, 5), (8, 55)]
        aims = [plan_at(T(h, m), "bathroom", away=(h, m) >= (7, 5)).aim for h, m in sweep]
        self.assertEqual(aims, [22.0, 22.0, 19.0, 19.0])

    def test_a_profile_without_a_lower_limit_falls_to_its_default_offset_while_away(self):
        family = CFG.zones["family"]
        zone = replace(family, profile=replace(family.profile, when_home=("person.mikkel",)))
        home = hp.plan(zone, inputs(), T(20), TZ)
        away = hp.plan(zone, inputs(away=True), T(20), TZ)
        self.assertEqual((home.mode, home.target, home.aim), ("hold", 22.0, 21.0))
        self.assertEqual((away.mode, away.target, away.aim), ("away", 21.5, 21.5))
        self.assertEqual(away.reasons, ("Mikkel away: holding 21.5 °C",))

    def test_a_zone_that_waits_for_nobody_ignores_the_flag(self):
        for zone in ("family", "kristines_room", "claudias_room", "guest_bathroom"):
            for hour in (3, 7, 12, 20):
                with self.subTest(zone=zone, hour=hour):
                    self.assertEqual(plan_at(T(hour), zone, away=True), plan_at(T(hour), zone))

    def test_away_allows_heat_and_the_gates_still_hold_it_back(self):
        p = plan_at(T(7), "bathroom", away=True)
        self.assertEqual((p.mode, p.allowed), ("away", True))
        for state, mode in ((hp.STATE_OPEN, "window"), (hp.STATE_SETTLE, "settle")):
            p = plan_at(T(7), "bathroom", away=True, window_state=state)
            self.assertEqual((p.mode, p.allowed, p.aim), (mode, False, 19.0))

    def test_the_season_and_the_device_gate_hide_the_mode_but_not_the_plan(self):
        gates = ((dict(season_on=False), "off_season"), (dict(online=False), "device_offline"))
        for kw, mode in gates:
            p = plan_at(T(7), "bathroom", away=True, **kw)
            self.assertEqual((p.mode, p.plan_mode, p.aim), (mode, "away", 19.0))

    def test_the_projection_shows_the_stretch_as_one_away_block(self):
        zc = CFG.zones["bathroom"]

        def span(inp):
            blocks = hp.project_blocks(zc, inp, T(0), T(0, day=18), TZ)
            return [(hp.hhmm(s, TZ), hp.hhmm(e, TZ), m, aim) for s, e, m, aim in blocks]

        self.assertEqual(
            span(inputs("bathroom")),
            [
                ("00:00", "06:00", "hold", 19.0),
                ("06:00", "09:00", "hold", 22.0),
                ("09:00", "00:00", "hold", 19.0),
            ],
        )
        self.assertEqual(span(inputs("bathroom", away=True)), [("00:00", "00:00", "away", 19.0)])

    def test_the_timeline_keeps_the_hours_at_home_and_shows_the_away_stretch(self):
        zc = CFG.zones["bathroom"]
        log = hp.BlockLog()
        log.observe(T(0), "2026-10-17", "hold", 19.0)
        log.observe(T(6), "2026-10-17", "hold", 22.0)
        log.observe(T(7, 30), "2026-10-17", "away", 19.0)
        projected = hp.project_blocks(zc, inputs("bathroom", away=True), T(7, 30), T(0, day=18), TZ)
        rows = hp.render_blocks(log, projected, T(7, 30), T(0, day=18), TZ)
        self.assertEqual(
            rows,
            [
                {"start": "00:00", "end": "06:00", "mode": "hold", "setpoint": "19.0"},
                {"start": "06:00", "end": "07:30", "mode": "hold", "setpoint": "22.0"},
                {"start": "07:30", "end": "24:00", "mode": "away", "setpoint": "19.0"},
            ],
        )


class FloorCap(unittest.TestCase):
    zone = replace(CFG.zones["family"], floor_max_c=26.0)

    def test_enters_at_the_maximum_and_releases_strictly_below_half_a_degree_less(self):
        active = False
        readings = (
            (25.9, False), (26.0, True), (26.5, True), (25.9, True),
            (25.5, True), (25.49, False), (25.9, False),
        )
        for floor, expected in readings:
            with self.subTest(floor=floor, active=active):
                active = hp.evaluate_floor_cap(self.zone.floor_max_c, floor, active)
                self.assertEqual(active, expected)
                result = hp.plan(self.zone, inputs(floor_cap=active), T(12), TZ)
                self.assertEqual(result.plan_mode, "floor_cap" if expected else "hold")

    def test_missing_floor_preserves_the_latch_and_unconfigured_limit_clears_it(self):
        self.assertFalse(hp.evaluate_floor_cap(26.0, None, False))
        self.assertTrue(hp.evaluate_floor_cap(26.0, None, True))
        self.assertFalse(hp.evaluate_floor_cap(None, 30.0, True))
        self.assertEqual(plan_at(T(12), floor_cap=True).plan_mode, "hold")

    def test_floor_cap_holds_heat_off_whatever_the_aim_or_sun_cut(self):
        for extra, aim in (({}, 21.5), ({"cut_windows": ((T(1), T(14), "2026-10-17"),)}, 20.5)):
            with self.subTest(extra=extra):
                result = hp.plan(self.zone, inputs(floor_cap=True, **extra), T(12), TZ)
                self.assertEqual(
                    (result.mode, result.allowed, result.aim), ("floor_cap", False, aim)
                )
                self.assertIn("until the floor is below 25.5", result.reasons[0])


class BudgetAndCut(unittest.TestCase):
    """The worked examples of the design, with the sun credit at full trust (factor 1)."""

    zc = CFG.zones["family"]

    def budget(self, weather, sun_hours, factor=1.0, zc=None):
        starts = [T(h) for h in sun_hours]
        return hp.day_budget(zc or self.zc, weather, starts, factor)

    def test_example_dull_cold_day_has_no_cut(self):
        b = self.budget(dw(2.0, 20.0, 0.4), [])
        self.assertAlmostEqual(b.h_dull, 11.8, delta=0.1)
        self.assertAlmostEqual(b.u_eq, 0.49, delta=0.01)
        self.assertAlmostEqual(b.s_sun, 0.6, delta=0.1)
        self.assertEqual(b.n_sun, 0)
        self.assertIsNone(b.window_start)
        self.assertEqual(hp.evaluate_cut(self.zc, [b], T(1), None).windows, ())

    def test_example_sunny_march_day_cuts(self):
        b = self.budget(dw(5.0, 10.0, 3.5), range(9, 15))
        self.assertAlmostEqual(b.h_dull, 8.0, delta=0.1)
        self.assertAlmostEqual(b.u_eq, 0.33, delta=0.01)
        self.assertAlmostEqual(b.s_sun, 5.7, delta=0.1)
        self.assertAlmostEqual(b.h_pred, 2.3, delta=0.1)
        self.assertEqual((b.window_start, b.window_end), (T(1), T(14)))
        self.assertAlmostEqual(b.skipped_h, 2.671, delta=0.01)
        self.assertAlmostEqual(b.ratio, 2.123, delta=0.01)
        cut = hp.evaluate_cut(self.zc, [b], T(2), None)
        self.assertEqual(cut.windows, ((T(1), T(14), "2026-10-17"),))
        self.assertEqual(cut.active_day, "2026-10-17")

    def test_example_sunny_december_day_does_not_cut(self):
        b = self.budget(dw(-1.0, 15.0, 1.2), range(11, 14))
        self.assertAlmostEqual(b.h_dull, 13.1, delta=0.1)
        self.assertAlmostEqual(b.u_eq, 0.55, delta=0.01)
        self.assertAlmostEqual(b.s_sun, 1.9, delta=0.1)
        self.assertAlmostEqual(b.ratio, 0.444, delta=0.01)
        self.assertEqual(hp.evaluate_cut(self.zc, [b], T(5), None).windows, ())

    def test_claudia_in_december_cuts_at_full_trust_and_not_at_half(self):
        zc = CFG.zones["claudias_room"]
        weather = dw(-1.0, 15.0, 1.2)
        b = self.budget(weather, [12, 13], zc=zc)
        self.assertAlmostEqual(b.h_dull, 11.3, delta=0.1)
        self.assertAlmostEqual(b.s_sun, 3.3, delta=0.1)
        self.assertEqual((b.window_start, b.window_end), (T(4), T(13)))
        self.assertAlmostEqual(b.ratio, 0.871, delta=0.01)
        self.assertEqual(len(hp.evaluate_cut(zc, [b], T(5), None).windows), 1)
        half = self.budget(weather, [12, 13], factor=0.5, zc=zc)
        self.assertAlmostEqual(half.ratio, 0.436, delta=0.01)
        self.assertEqual(hp.evaluate_cut(zc, [half], T(5), None).windows, ())

    def test_half_trust_only_changes_the_credit_not_the_expectation(self):
        full = self.budget(dw(5.0, 10.0, 3.5), range(9, 15), factor=1.0)
        half = self.budget(dw(5.0, 10.0, 3.5), range(9, 15), factor=0.5)
        self.assertAlmostEqual(half.s_credit, full.s_sun / 2)
        self.assertEqual(half.s_sun, full.s_sun)
        self.assertEqual(half.h_pred, full.h_pred)
        self.assertAlmostEqual(half.ratio, full.ratio / 2)
        # the sunny March day is still worth a cut at half trust
        self.assertEqual(len(hp.evaluate_cut(self.zc, [half], T(2), None).windows), 1)

    def test_the_window_needs_two_sun_hours(self):
        b = self.budget(dw(5.0, 10.0, 3.5), [12])
        self.assertEqual(hp.evaluate_cut(self.zc, [b], T(2), None).windows, ())

    def test_the_ratio_does_not_fall_as_the_sun_hours_rise(self):
        short = self.budget(dw(5.0, 10.0, 3.5), [11, 12])
        long = self.budget(dw(5.0, 10.0, 3.5), range(9, 15))
        self.assertGreater(
            long.window_end - long.window_start, short.window_end - short.window_start
        )
        self.assertEqual((short.skipped_h, short.ratio), (long.skipped_h, long.ratio))
        self.assertEqual(short.skipped_h, short.u_eq * self.zc.sun_cut.before_hours)
        for b in (short, long):
            self.assertEqual(len(hp.evaluate_cut(self.zc, [b], T(2), None).windows), 1)

    def test_a_day_with_hardly_any_heat_to_skip_is_not_cut(self):
        b = self.budget(dw(16.5, 5.0, 3.5), range(9, 15))
        self.assertLess(b.skipped_h, self.zc.sun_cut.min_skipped_h)
        self.assertEqual(hp.evaluate_cut(self.zc, [b], T(2), None).windows, ())

    def test_a_running_cut_ends_early_only_below_the_lower_threshold(self):
        b = self.budget(dw(5.0, 10.0, 3.5), range(9, 15))
        weaker = replace(b, ratio=0.40)
        self.assertEqual(hp.evaluate_cut(self.zc, [weaker], T(2), None).windows, ())
        self.assertEqual(len(hp.evaluate_cut(self.zc, [weaker], T(2), "2026-10-17").windows), 1)
        gone = replace(b, ratio=0.30)
        cut = hp.evaluate_cut(self.zc, [gone], T(2), "2026-10-17")
        self.assertEqual((cut.windows, cut.active_day), ((), None))

    def test_a_window_not_started_yet_qualifies_but_is_not_running(self):
        b = self.budget(dw(5.0, 10.0, 3.5), range(9, 15))
        cut = hp.evaluate_cut(self.zc, [b], T(0, 30), None)
        self.assertEqual(len(cut.windows), 1)
        self.assertIsNone(cut.active_day)

    def test_a_window_that_has_ended_is_not_running(self):
        b = self.budget(dw(5.0, 10.0, 3.5), range(9, 15))
        self.assertIsNone(hp.evaluate_cut(self.zc, [b], T(14), None).active_day)

    def test_two_days_give_two_windows(self):
        d1, d2 = DAY, date(2026, 10, 18)
        b1 = self.budget(dw(5.0, 10.0, 3.5, d1), range(9, 15))
        sun2 = [T(h, day=18) for h in range(9, 15)]
        b2 = hp.day_budget(self.zc, dw(5.0, 10.0, 3.5, d2), sun2, 1.0)
        cut = hp.evaluate_cut(self.zc, [b1, b2], T(2), None)
        self.assertEqual([w[2] for w in cut.windows], ["2026-10-17", "2026-10-18"])

    def test_sun_cut_disabled_zone_has_a_budget_but_no_window(self):
        zc = CFG.zones["bathroom"]
        b = self.budget(dw(2.0, 10.0, 1.0), range(9, 15), zc=zc)
        self.assertIsNotNone(b.h_pred)
        self.assertIsNone(b.window_start)
        self.assertEqual(hp.evaluate_cut(zc, [b], T(2), None).windows, ())

    def test_changepoint_budget_for_a_room_with_no_weather_model(self):
        zc = CFG.zones["kristines_room"]
        b = self.budget(dw(5.0, 10.0, 3.5), [], zc=zc)
        self.assertAlmostEqual(b.h_dull, 0.17 * 10.5)
        self.assertEqual(b.s_sun, 0.0)
        self.assertEqual(b.h_pred, b.h_dull)
        warm = self.budget(dw(18.0, 10.0, 3.5), [], zc=zc)
        self.assertEqual(warm.h_dull, 0.0)

    def test_zone_without_a_budget_gets_only_the_weather(self):
        b = self.budget(dw(5.0, 10.0, 3.5), [], zc=CFG.zones["guest_bathroom"])
        self.assertIsNone(b.h_dull)
        self.assertEqual(b.t_mean, 5.0)

    def test_duty_is_limited_to_a_full_day(self):
        b = self.budget(dw(-20.0, 30.0, 0.1), [])
        self.assertEqual(b.u_eq, 1.0)


class PartyWindows(unittest.TestCase):
    pc = CFG.party

    def state(self, now, start=None, end=None, on=False, since=None):
        return hp.party_state(now, self.pc, start, end, on, since)

    def test_planned_party_pauses_the_zone_eight_hours_ahead(self):
        start, end = T(19), T(23)
        self.assertFalse(self.state(T(10, 59), start, end).pause)
        s = self.state(T(11), start, end)
        self.assertEqual((s.pause, s.in_event, s.source), (True, False, "planned"))
        self.assertEqual(s.event_end, end)
        s = self.state(T(19), start, end)
        self.assertEqual((s.pause, s.in_event), (True, True))
        self.assertEqual(self.state(T(23), start, end).pause, False)

    def test_missing_end_or_end_before_start_means_six_hours(self):
        start = T(19)
        for end in (None, T(18)):
            s = self.state(T(20), start, end)
            self.assertEqual(s.event_end, start + 6 * 3600, end)
            self.assertEqual(self.state(T(0, 59, day=18), start, end).pause, True)
            self.assertEqual(self.state(T(1, day=18), start, end).pause, False)

    def test_start_and_end_at_the_same_moment_is_two_untouched_helpers(self):
        self.assertEqual(self.state(T(0, 30), T(0), T(0)), hp.PartyState())

    def test_an_end_more_than_a_day_after_the_start_is_a_stale_helper(self):
        start = T(19)
        s = self.state(T(20), start, start + 25 * 3600)
        self.assertEqual(s.event_end, start + 6 * 3600)
        s = self.state(T(20), start, start + 24 * 3600)
        self.assertEqual(s.event_end, start + 24 * 3600)

    def test_an_unset_datetime_helper_is_ignored(self):
        epoch0 = datetime(1970, 1, 1).timestamp()
        s = self.state(T(12), epoch0, epoch0)
        self.assertEqual((s.pause, s.windows), (False, ()))
        self.assertEqual(self.state(T(12), None, None).pause, False)

    def test_manual_switch_runs_until_it_is_switched_off_or_twelve_hours(self):
        since = T(20)
        s = self.state(T(21), on=True, since=since)
        self.assertEqual((s.pause, s.in_event, s.source), (True, True, "manual"))
        self.assertEqual(self.state(T(7, 59, day=18), on=True, since=since).pause, True)
        self.assertEqual(self.state(T(8, day=18), on=True, since=since).pause, False)
        self.assertEqual(self.state(T(21), on=False, since=since).pause, False)

    def test_planned_and_manual_together(self):
        s = self.state(T(20), T(19), T(23), on=True, since=T(20))
        self.assertEqual(s.source, "planned+manual")
        self.assertEqual(s.event_end, T(8, day=18))  # the later of the two ends
        self.assertEqual(len(s.windows), 2)

    def test_windows_in_the_past_are_dropped_from_the_plan_input(self):
        s = self.state(T(23, 30), T(19), T(23))
        self.assertEqual(s.windows, ())

    def test_plan_uses_the_windows(self):
        s = self.state(T(12), T(19), T(23))
        p = plan_at(T(12), party_windows=s.windows)
        self.assertEqual((p.mode, p.allowed, p.aim), ("party", False, 21.5))
        self.assertIn("until 23:00", p.reasons[0])


class AiringSuggestion(unittest.TestCase):
    pc = CFG.party
    party = hp.PartyState(pause=True, in_event=True, source="planned", event_end=T(23))

    def suggest(self, air, band=None, last=None, now=T(20), state=None):
        return hp.airing_suggestion(self.pc, now, state or self.party, air, 21.5, band, last)

    def test_warm_air_triggers(self):
        msg = self.suggest(23.6)
        self.assertEqual(msg, "Living area 23.6 °C: open the terrace doors for 10 min")

    def test_stuffy_air_triggers_and_both_reasons_are_named(self):
        self.assertEqual(
            self.suggest(21.5, "stuffy"),
            "Living area air stuffy: open the terrace doors for 10 min",
        )
        self.assertIn("23.6 °C, air poor", self.suggest(23.6, "poor"))

    def test_comfortable_and_fresh_is_quiet(self):
        self.assertIsNone(self.suggest(22.4, "good"))
        self.assertIsNone(self.suggest(None, "unknown"))

    def test_one_suggestion_every_forty_five_minutes(self):
        self.assertIsNone(self.suggest(24.0, last=T(20) - 44 * 60))
        self.assertIsNotNone(self.suggest(24.0, last=T(20) - 45 * 60))

    def test_only_while_the_party_is_on_not_during_the_lead_in(self):
        lead_in = hp.PartyState(pause=True, in_event=False, source="planned")
        self.assertIsNone(self.suggest(25.0, state=lead_in))
        self.assertIsNone(self.suggest(25.0, state=hp.PartyState()))


def evening(loop, airs, day=17, window_open_at=None, stale=False, ceiling=21.0):
    """Drive the loop through an evening in 5-minute steps from 21:00 and close it at 23:30.
    `airs` is one reading per step or a single value. Returns the decision."""
    steps = list(range(21 * 60, 23 * 60 + 30, 5))
    if not isinstance(airs, (list, tuple)):
        airs = [airs] * len(steps)
    for i, minute in enumerate(steps):
        opened = window_open_at is not None and i >= window_open_at
        loop.observe(T(minute // 60, minute % 60, day=day), TZ, airs[i], stale, opened, ceiling)
    return loop.observe(T(23, 30, day=day), TZ, airs[-1], stale, False, ceiling)


class BedroomLoopTests(unittest.TestCase):
    cfg = hp.BedroomConfig()

    def loop(self, hold=False):
        return hp.BedroomLoop(self.cfg, {"hold": hold})

    def test_ceiling_by_time_of_night(self):
        at = lambda h, m=0: hp.bedroom_ceiling(self.cfg, h * 60 + m, 21.0)  # noqa: E731
        self.assertIsNone(at(12))
        self.assertIsNone(at(20, 59))
        self.assertEqual(at(21), 21.0)
        self.assertEqual(at(23, 30), 21.0)
        self.assertEqual(at(2, 59), 21.0)
        self.assertEqual(at(3), 21.5)
        self.assertEqual(at(5, 59), 21.5)
        self.assertIsNone(at(6))

    def test_a_warm_evening_holds_the_bathroom_back(self):
        loop = self.loop()
        d = evening(loop, 21.4)
        self.assertEqual(
            (d["action"], d["hold"], d["mean"], d["day"]), ("hold", True, 21.4, "2026-10-17")
        )
        self.assertTrue(loop.hold)

    def test_a_mean_exactly_at_the_ceiling_is_not_too_warm(self):
        self.assertEqual(evening(self.loop(), 21.0)["action"], "keep")

    def test_a_cool_evening_releases_the_hold(self):
        loop = self.loop(hold=True)
        d = evening(loop, 20.3)
        self.assertEqual((d["action"], d["hold"]), ("release", False))
        self.assertFalse(loop.hold)

    def test_the_band_between_keeps_whatever_was_decided(self):
        for before in (True, False):
            loop = self.loop(hold=before)
            d = evening(loop, 20.8)
            self.assertEqual((d["action"], d["hold"]), ("keep", before))
        self.assertEqual(evening(self.loop(hold=True), 20.5)["action"], "keep")

    def test_the_mean_not_a_single_reading_decides(self):
        airs = [22.5] * 3 + [20.2] * 27  # three warm readings do not outweigh a cool evening
        d = evening(self.loop(hold=True), airs)
        self.assertEqual(d["action"], "release")
        self.assertAlmostEqual(d["mean"], 20.43, places=2)

    def test_airing_before_bed_is_deliberate_cooling_and_decides_nothing(self):
        # window open from 22:20 on: not read as a cooled-down room, not as a warm one
        for before in (True, False):
            for air in (19.0, 22.5):
                loop = self.loop(hold=before)
                d = evening(loop, air, window_open_at=16)
                self.assertEqual((d["action"], d["hold"]), ("keep_airing", before), (before, air))
                self.assertEqual(loop.hold, before)

    def test_airing_before_the_window_does_not_matter_and_one_blip_does(self):
        loop = self.loop()
        steps = list(range(21 * 60, 23 * 60 + 30, 5))
        for minute in steps:
            blip = minute == 22 * 60
            loop.observe(T(minute // 60, minute % 60), TZ, 21.4, False, blip, 21.0)
        d = loop.observe(T(23, 30), TZ, 21.4, False, False, 21.0)
        self.assertEqual(d["action"], "keep_airing")

    def test_missing_or_stale_readings_count_as_warm(self):
        d = evening(self.loop(hold=False), 20.0, stale=True)
        self.assertEqual((d["action"], d["hold"], d["samples"]), ("hold_no_data", True, 0))
        d = evening(self.loop(), [None] * 30)
        self.assertEqual(d["action"], "hold_no_data")
        # a few readings are still too few
        airs = [20.0] * 5 + [None] * 25
        self.assertEqual(evening(self.loop(), airs)["action"], "hold_no_data")
        airs = [20.0] * 6 + [None] * 24
        self.assertEqual(evening(self.loop(hold=True), airs)["action"], "release")

    def test_nothing_is_decided_before_the_window_closes(self):
        loop = self.loop()
        for minute in range(21 * 60, 23 * 60 + 29, 5):
            self.assertIsNone(
                loop.observe(T(minute // 60, minute % 60), TZ, 22.0, False, False, 21.0)
            )
        self.assertIsNone(loop.observe(T(23, 29), TZ, 22.0, False, False, 21.0))
        self.assertIsNotNone(loop.observe(T(23, 30), TZ, 22.0, False, False, 21.0))

    def test_an_evening_is_decided_once(self):
        loop = self.loop()
        evening(loop, 21.4)
        self.assertIsNone(loop.observe(T(23, 35), TZ, 21.4, False, False, 21.0))
        self.assertIsNone(loop.observe(T(23, 55), TZ, 21.4, False, False, 21.0))
        self.assertIsNone(loop.observe(T(12, day=18), TZ, 21.4, False, False, 21.0))

    def test_readings_outside_the_window_are_not_counted(self):
        loop = self.loop()
        loop.observe(T(20, 55), TZ, 25.0, False, False, 21.0)
        d = evening(loop, 20.0)
        self.assertEqual(d["mean"], 20.0)

    def test_readings_closer_than_four_minutes_count_once(self):
        loop = self.loop()
        for second in range(0, 240, 30):
            loop.observe(T(21, 0) + second, TZ, 20.0, False, False, 21.0)
        self.assertEqual(loop.count, 1)

    def test_an_evening_missed_by_a_restart_is_decided_when_the_app_is_back(self):
        loop = self.loop()
        for minute in range(21 * 60, 23 * 60 + 25, 5):
            loop.observe(T(minute // 60, minute % 60), TZ, 21.4, False, False, 21.0)
        saved = loop.snapshot()
        back = hp.BedroomLoop(self.cfg, saved)
        d = back.observe(T(0, 10, day=18), TZ, 21.0, False, False, 21.0)
        self.assertEqual((d["action"], d["day"]), ("hold", "2026-10-17"))

    def test_state_survives_a_snapshot_round_trip(self):
        loop = self.loop()
        evening(loop, 21.4)
        again = hp.BedroomLoop(self.cfg, loop.snapshot())
        self.assertTrue(again.hold)
        self.assertEqual(again.last["action"], "hold")
        self.assertEqual(again.decided, "2026-10-17")

    def test_the_next_evening_starts_from_zero(self):
        loop = self.loop()
        evening(loop, 22.0)
        d = evening(loop, 20.0, day=18)
        self.assertEqual((d["day"], d["mean"], d["action"]), ("2026-10-18", 20.0, "release"))

    def test_a_cold_spell_and_a_warm_one_settle_into_the_band(self):
        loop = self.loop()
        actions = [
            evening(loop, air, day=day)["action"]
            for day, air in ((17, 21.4), (18, 20.8), (19, 20.3), (20, 20.8))
        ]
        self.assertEqual(actions, ["hold", "keep", "release", "keep"])
        self.assertFalse(loop.hold)


class ReadingGuardTests(unittest.TestCase):
    def test_out_of_range_and_junk_are_rejected(self):
        g = hp.ReadingGuard()
        for bad in (4.9, 35.1, 12.34 * 10, None, "unknown", "unavailable", "x"):
            self.assertIsNone(g.filter("a", bad, 0), bad)
        self.assertEqual(g.filter("a", "21.5", 0), 21.5)

    def test_a_jump_inside_ten_minutes_holds_the_last_value(self):
        g = hp.ReadingGuard()
        self.assertEqual(g.filter("a", 21.0, 0), 21.0)
        self.assertEqual(g.filter("a", 24.0, 300), 21.0)
        self.assertEqual(g.filter("a", 21.8, 301), 21.8)

    def test_a_jump_after_ten_minutes_is_accepted(self):
        g = hp.ReadingGuard()
        g.filter("a", 21.0, 0)
        self.assertEqual(g.filter("a", 24.0, 600), 24.0)

    def test_an_open_window_allows_fast_moves(self):
        g = hp.ReadingGuard()
        g.filter("a", 21.0, 0)
        self.assertEqual(g.filter("a", 18.0, 120, window_open=True), 18.0)

    def test_keys_are_independent(self):
        g = hp.ReadingGuard()
        g.filter("a", 21.0, 0)
        self.assertEqual(g.filter("b", 25.0, 10), 25.0)


class ControlAirChain(unittest.TestCase):
    def test_room_readings_come_first_and_are_averaged(self):
        self.assertEqual(hp.resolve_control_air([22.0, 23.0], (20.0, 5), (19.0, 5)), (22.5, "feel"))
        self.assertEqual(hp.resolve_control_air([None, 23.0], None, None), (23.0, "feel"))

    def test_then_the_thermostat_then_the_floor_each_only_when_fresh(self):
        self.assertEqual(
            hp.resolve_control_air([None], (21.0, 180), (19.0, 5)), (21.0, "thermostat")
        )
        self.assertEqual(hp.resolve_control_air([None], (21.0, 181), (19.0, 180)), (19.5, "floor"))
        self.assertEqual(hp.resolve_control_air([None], (21.0, 181), (19.0, 181)), (None, "none"))
        self.assertEqual(hp.resolve_control_air([], None, None), (None, "none"))


class WeightedAir(unittest.TestCase):
    def test_a_room_counts_by_its_weight(self):
        self.assertAlmostEqual(hp.weighted_mean([26.0, 22.0, 22.0], (19.0, 18.5, 18.5)), 1308 / 56)
        self.assertEqual(hp.weighted_mean([20.0, 24.0], (3.0, 1.0)), 21.0)

    def test_without_weights_it_is_the_plain_mean(self):
        for values in ([22.0, 23.0], [21.4, 22.6, 22.0], [22.2]):
            plain = sum(values) / len(values)
            self.assertEqual(hp.weighted_mean(values), plain)
            self.assertEqual(hp.weighted_mean(values, (1.0,) * len(values)), plain)

    def test_a_room_that_does_not_read_drops_out_and_the_rest_renormalise(self):
        weights = (19.0, 18.5, 18.5)
        self.assertAlmostEqual(
            hp.weighted_mean([26.0, None, 22.0], weights), (19 * 26.0 + 18.5 * 22.0) / 37.5
        )
        self.assertEqual(hp.weighted_mean([None, 22.0, None], weights), 22.0)
        self.assertIsNone(hp.weighted_mean([None, None, None], weights))
        self.assertIsNone(hp.weighted_mean([], weights))

    def test_the_chain_weighs_only_the_rooms_and_the_fallbacks_stay_as_they_were(self):
        weighted = hp.resolve_control_air([20.0, 24.0], None, None, (3.0, 1.0))
        self.assertEqual(weighted, (21.0, "feel"))
        self.assertEqual(hp.resolve_control_air([20.0, 24.0], None, None), (22.0, "feel"))
        self.assertEqual(
            hp.resolve_control_air([None, None], (21.0, 5), (19.0, 5), (3.0, 1.0)),
            (21.0, "thermostat"),
        )
        self.assertEqual(
            hp.resolve_control_air([None, None], None, (19.0, 5), (3.0, 1.0)), (19.5, "floor")
        )


class ComfortPick(unittest.TestCase):
    TARGET = 21.5

    def pick(self, means, active=None, target=TARGET):
        return hp.comfort_pick(means, target, active)

    def test_rooms_within_the_band_leave_the_weighted_mean_in_charge(self):
        self.assertIsNone(self.pick({"a": 21.5, "b": 21.0, "c": 22.0}))
        self.assertIsNone(self.pick({}))

    def test_a_room_more_than_seven_tenths_off_takes_over_on_either_side(self):
        self.assertEqual(self.pick({"a": 20.79, "b": 21.5}), "a")
        self.assertEqual(self.pick({"a": 21.5, "b": 22.21}), "b")

    def test_exactly_seven_tenths_is_not_off(self):
        self.assertIsNone(self.pick({"a": 20.8, "b": 22.2}))
        self.assertIsNone(self.pick({"a": 20.8}))
        self.assertIsNone(self.pick({"a": 22.2}))

    def test_the_active_room_stays_until_it_is_back_within_four_tenths(self):
        self.assertEqual(self.pick({"a": 21.0, "b": 21.5}, active="a"), "a")
        self.assertEqual(self.pick({"a": 22.0, "b": 21.5}, active="a"), "a")
        self.assertIsNone(self.pick({"a": 21.11, "b": 21.5}, active="a"))
        self.assertIsNone(self.pick({"a": 21.1, "b": 21.5}, active="a"))
        self.assertIsNone(self.pick({"a": 21.89, "b": 21.5}, active="a"))
        self.assertIsNone(self.pick({"a": 21.0, "b": 21.5}))

    def test_of_several_rooms_off_on_one_side_the_active_one_stays_else_the_furthest_wins(self):
        self.assertEqual(self.pick({"a": 21.0, "b": 20.0}, active="a"), "a")
        self.assertEqual(self.pick({"a": 20.4, "b": 20.0, "c": 21.5}), "b")
        self.assertEqual(self.pick({"a": 20.0, "b": 20.4}), "a")
        self.assertEqual(self.pick({"a": 22.5, "b": 22.9}), "b")
        self.assertEqual(self.pick({"a": 20.5, "b": 20.5}), "a")

    def test_rooms_off_on_both_sides_are_a_conflict_the_weighted_mean_settles(self):
        both = {"a": 20.5, "b": 22.5, "c": 21.5}
        self.assertIsNone(self.pick(both))
        self.assertIsNone(self.pick(both, active="a"))
        self.assertIsNone(self.pick({"a": 21.0, "b": 22.5}, active="a"))

    def test_the_pick_starts_over_once_one_side_is_back(self):
        self.assertEqual(self.pick({"a": 20.5, "b": 21.5, "c": 21.5}), "a")
        self.assertEqual(self.pick({"a": 21.0, "b": 22.5}), "b")

    def test_a_target_that_is_not_a_number_picks_nothing(self):
        for target in (float("nan"), float("inf"), -float("inf")):
            self.assertIsNone(hp.comfort_pick({"a": 10.0}, target, None))
            self.assertIsNone(hp.comfort_pick({"a": 10.0}, target, "a"))


class ComfortSwitchTests(unittest.TestCase):
    A, B = "sensor.a", "sensor.b"
    T0 = T(10)
    STEP = 300.0
    TARGET = 21.5

    def switch(self, saved=None):
        return hp.ComfortSwitch((self.A, self.B), saved)

    def feed(self, switch, count, a, b, start=None, record=True):
        """count readings five minutes apart from `start`; returns the time of the last."""
        start = self.T0 if start is None else start
        for i in range(count):
            switch.observe(start + i * self.STEP, {self.A: a, self.B: b}, record)
        return start + (count - 1) * self.STEP

    def test_a_reading_is_kept_at_most_once_per_sample_gap(self):
        switch = self.switch()
        gap = hp.COMFORT_SAMPLE_GAP_S
        for offset in (0, 100, gap - 1, gap, gap + 100, 2 * gap):
            switch.observe(self.T0 + offset, {self.A: 20.0, self.B: None})
        self.assertEqual(
            [ts for ts, _v in switch.samples[self.A]], [self.T0, self.T0 + gap, self.T0 + 2 * gap]
        )
        self.assertEqual(switch.samples[self.B], [])

    def test_a_mean_counts_only_on_enough_readings(self):
        switch = self.switch()
        last = self.feed(switch, hp.COMFORT_MIN_SAMPLES - 1, 20.0, 21.5)
        self.assertEqual(switch.means(last), {})
        last = self.feed(switch, 1, 20.0, 21.5, start=last + self.STEP)
        self.assertEqual(switch.means(last), {self.A: 20.0, self.B: 21.5})

    def test_a_reading_leaves_the_window_after_three_hours(self):
        switch = self.switch()
        switch.observe(self.T0, {self.A: 10.0})
        self.feed(switch, 36, 20.0, 21.5, start=self.T0 + self.STEP)
        end = self.T0 + 36 * self.STEP
        self.assertEqual(len(switch.samples[self.A]), 36)
        self.assertEqual(switch.means(end), {self.A: 20.0, self.B: 21.5})
        switch.observe(self.T0 + hp.COMFORT_WINDOW_S, {})
        self.assertEqual(switch.samples[self.A][0][0], self.T0 + self.STEP)

    def test_an_airing_is_not_recorded_but_the_window_still_moves(self):
        switch = self.switch()
        last = self.feed(switch, 30, 20.0, 21.5)
        before = [list(s) for s in switch.samples[self.A]]
        later = last + 2 * 3600.0
        switch.observe(later, {self.A: 5.0, self.B: 5.0}, record=False)
        cutoff = later - hp.COMFORT_WINDOW_S
        self.assertEqual(switch.samples[self.A], [s for s in before if s[0] > cutoff])
        self.assertNotIn(5.0, [v for _t, v in switch.samples[self.A]])

    def test_a_cold_start_does_not_judge_a_room_on_a_few_readings(self):
        switch = self.switch()
        last = self.feed(switch, hp.COMFORT_MIN_SAMPLES - 1, 15.0, 21.5)
        self.assertIsNone(switch.decide(last, self.TARGET))

    def test_a_room_that_stays_off_takes_over_and_the_pick_is_kept_with_the_mean(self):
        switch = self.switch()
        last = self.feed(switch, hp.COMFORT_MIN_SAMPLES, 20.0, 21.5)
        self.assertEqual(switch.decide(last, self.TARGET), self.A)
        self.assertEqual(switch.active, self.A)
        self.assertEqual(switch.decide(last, self.TARGET), self.A)

    def test_it_is_released_when_the_mean_is_back_within_four_tenths(self):
        switch = self.switch()
        last = self.feed(switch, hp.COMFORT_MIN_SAMPLES, 20.0, 21.5)
        switch.decide(last, self.TARGET)
        previous_gap = None
        for _ in range(60):
            last = self.feed(switch, 1, 21.5, 21.5, start=last + self.STEP)
            gap = abs(switch.means(last)[self.A] - self.TARGET)
            if switch.decide(last, self.TARGET) is None:
                break
            previous_gap = gap
        self.assertIsNone(switch.active)
        self.assertLessEqual(gap, hp.COMFORT_RELEASE_C)
        self.assertGreater(previous_gap, hp.COMFORT_RELEASE_C)

    def test_two_rooms_off_on_opposite_sides_keep_the_weighted_mean(self):
        switch = self.switch()
        last = self.feed(switch, hp.COMFORT_MIN_SAMPLES, 20.0, 23.0)
        self.assertIsNone(switch.decide(last, self.TARGET))

    def test_a_pick_is_dropped_when_its_room_has_no_mean_any_more(self):
        switch = self.switch()
        last = self.feed(switch, hp.COMFORT_MIN_SAMPLES, 20.0, 21.5)
        switch.decide(last, self.TARGET)
        later = last + hp.COMFORT_WINDOW_S + 60.0
        switch.observe(later, {self.A: 20.0, self.B: 21.5})
        self.assertEqual(switch.means(later), {})
        self.assertIsNone(switch.decide(later, self.TARGET))

    def test_a_restart_carries_the_readings_and_the_pick_over_unchanged(self):
        switch = self.switch()
        last = self.feed(switch, hp.COMFORT_MIN_SAMPLES + 5, 20.0, 21.5)
        switch.decide(last, self.TARGET)
        restored = self.switch(json.loads(json.dumps(switch.snapshot())))
        self.assertEqual(restored.active, self.A)
        self.assertEqual(restored.means(last), switch.means(last))
        self.assertEqual(restored.decide(last + 60.0, self.TARGET), self.A)

    def test_a_restart_without_saved_readings_waits_instead_of_switching(self):
        restored = self.switch({})
        self.assertIsNone(restored.active)
        last = self.feed(restored, hp.COMFORT_MIN_SAMPLES - 1, 20.0, 21.5)
        self.assertIsNone(restored.decide(last, self.TARGET))

    def test_junk_in_the_saved_state_is_ignored(self):
        saved = {
            "samples": {
                self.A: [
                    [self.T0, 20.0], [1, 2, 3], ["x", 1], [float("nan"), 1], [True, 1],
                    None, [self.T0 + 60.0, float("inf")], [self.T0 - 60.0, 19.0],
                ],
                "sensor.gone": [[self.T0, 20.0]],
                self.B: "junk",
            },
            "active": "sensor.gone",
        }
        switch = self.switch(saved)
        kept = {self.A: [[self.T0 - 60.0, 19.0], [self.T0, 20.0]], self.B: []}
        self.assertEqual(switch.samples, kept)
        self.assertIsNone(switch.active)
        for junk in (None, [], "x", {"samples": []}, {"samples": {}, "active": ["sensor.a"]}):
            self.assertIsNone(self.switch(junk).active)

    def test_readings_from_the_future_are_dropped(self):
        switch = self.switch({"samples": {self.A: [[self.T0 + 3600.0, 20.0]]}})
        switch.observe(self.T0, {self.A: 21.0})
        self.assertEqual(switch.samples[self.A], [[self.T0, 21.0]])


class WindowTrackerTests(unittest.TestCase):
    A, B = "binary_sensor.a", "binary_sensor.b"

    def tracker(self, **kw):
        return hp.WindowTracker(hp.WindowConfig(**kw))

    def drive(self, tr, t0, steps):
        """steps: list of (seconds from t0, {entity: state}, air) -> list of (t, event)."""
        events = []
        for sec, openings, air in steps:
            ev = tr.update(t0 + sec, openings, air)
            if ev:
                events.append((sec, ev))
        return events

    def test_an_opening_must_stay_on_for_two_minutes(self):
        tr = self.tracker()
        t0 = T(12)
        self.assertIsNone(tr.update(t0, {self.A: "on"}, 21.5))
        self.assertIsNone(tr.update(t0 + 119, {self.A: "on"}, 21.5))
        self.assertEqual(tr.state, "closed")
        self.assertEqual(tr.update(t0 + 120, {self.A: "on"}, 21.4), hp.EVENT_OPENED)
        self.assertEqual((tr.state, tr.pre_open_air, tr.opened_at), ("open", 21.4, t0))

    def test_a_blip_never_opens(self):
        tr = self.tracker()
        t0 = T(12)
        tr.update(t0, {self.A: "on"}, 21.5)
        tr.update(t0 + 60, {self.A: "off"}, 21.5)
        self.assertIsNone(tr.update(t0 + 300, {self.A: "off"}, 21.5))
        self.assertEqual(tr.state, "closed")

    def test_a_short_airing_settles_then_restores_when_the_air_recovers(self):
        tr = self.tracker()
        t0 = T(12)
        events = self.drive(
            tr,
            t0,
            [
                (0, {self.A: "on"}, 21.5),
                (120, {self.A: "on"}, 21.4),
                (600, {self.A: "off"}, 20.0),  # closed after 10 min: a deliberate airing
                (900, {self.A: "off"}, 20.8),  # 21.4 - 0.3 = 21.1 not reached yet
                (1500, {self.A: "off"}, 21.1),
            ],
        )
        self.assertEqual(
            events, [(120, hp.EVENT_OPENED), (600, hp.EVENT_SETTLE), (1500, hp.EVENT_RESTORED)]
        )
        self.assertEqual(tr.state, "closed")
        self.assertIsNone(tr.pre_open_air)

    def test_settle_ends_after_an_hour_if_the_air_never_recovers(self):
        tr = self.tracker()
        t0 = T(12)
        self.drive(
            tr,
            t0,
            [(0, {self.A: "on"}, 21.5), (120, {self.A: "on"}, 21.5), (600, {self.A: "off"}, 19.0)],
        )
        self.assertEqual(tr.state, "settle")
        self.assertIsNone(tr.update(t0 + 600 + 3599, {self.A: "off"}, 19.0))
        self.assertEqual(tr.update(t0 + 600 + 3600, {self.A: "off"}, 19.0), hp.EVENT_RESTORED)

    def test_settle_without_a_known_air_only_times_out(self):
        tr = self.tracker()
        t0 = T(12)
        self.drive(
            tr,
            t0,
            [(0, {self.A: "on"}, None), (120, {self.A: "on"}, None), (600, {self.A: "off"}, None)],
        )
        self.assertIsNone(tr.update(t0 + 1200, {self.A: "off"}, 25.0))
        self.assertEqual(tr.update(t0 + 600 + 3600, {self.A: "off"}, 25.0), hp.EVENT_RESTORED)

    def test_every_close_settles_however_long_the_opening_was(self):
        for minutes in (10, 30, 31, 180, 600):
            with self.subTest(minutes=minutes):
                tr = self.tracker()
                t0 = T(12)
                events = self.drive(
                    tr,
                    t0,
                    [
                        (0, {self.A: "on"}, 21.5),
                        (120, {self.A: "on"}, 21.5),
                        (minutes * 60, {self.A: "off"}, 18.0),
                    ],
                )
                self.assertEqual(events[-1], (minutes * 60, hp.EVENT_SETTLE))
                self.assertEqual(tr.state, "settle")

    def test_reopening_during_settle_keeps_the_original_air_to_recover_to(self):
        tr = self.tracker()
        t0 = T(12)
        self.drive(
            tr,
            t0,
            [(0, {self.A: "on"}, 21.5), (120, {self.A: "on"}, 21.5), (600, {self.A: "off"}, 20.0)],
        )
        ev = self.drive(tr, t0, [(700, {self.A: "on"}, 20.2), (820, {self.A: "on"}, 20.2)])
        self.assertEqual(ev, [(820, hp.EVENT_OPENED)])
        self.assertEqual(tr.pre_open_air, 21.5)
        self.assertEqual(tr.opened_at, t0)

    def test_two_openings_the_zone_waits_for_the_last_one(self):
        tr = self.tracker()
        t0 = T(12)
        both = {self.A: "on", self.B: "on"}
        self.drive(tr, t0, [(0, both, 21.5), (120, both, 21.5)])
        self.assertEqual(tr.state, "open")
        self.assertIsNone(tr.update(t0 + 400, {self.A: "off", self.B: "on"}, 21.0))
        self.assertEqual(tr.state, "open")
        self.assertEqual(tr.update(t0 + 500, {self.A: "off", self.B: "off"}, 21.0), hp.EVENT_SETTLE)

    def test_unavailable_edges_do_nothing(self):
        tr = self.tracker()
        t0 = T(12)
        for sec, st in (
            (0, "unavailable"),
            (60, "off"),
            (700, "unavailable"),
            (800, "unknown"),
            (900, "off"),
        ):
            self.assertIsNone(tr.update(t0 + sec, {self.A: st}, 21.5), (sec, st))
        self.assertEqual(tr.state, "closed")

    def test_an_opening_that_comes_back_on_counts_from_when_it_was_seen_on(self):
        tr = self.tracker()
        t0 = T(12)
        tr.update(t0, {self.A: "unavailable"}, 21.5)
        self.assertIsNone(tr.update(t0 + 600, {self.A: "on"}, 21.5))
        self.assertEqual(tr.update(t0 + 720, {self.A: "on"}, 21.5), hp.EVENT_OPENED)

    def test_a_dead_contact_cannot_pin_the_room(self):
        tr = self.tracker()
        t0 = T(12)
        self.drive(tr, t0, [(0, {self.A: "on"}, 21.5), (120, {self.A: "on"}, 21.5)])
        self.assertEqual(tr.state, "open")
        self.assertIsNone(tr.update(t0 + 700, {self.A: "unavailable"}, 20.0))
        self.assertEqual(
            tr.state, "open"
        )  # unavailable for under ten minutes: keeps its last state
        self.assertIsNone(tr.update(t0 + 1299, {self.A: "unavailable"}, 20.0))
        self.assertEqual(tr.update(t0 + 1300, {self.A: "unavailable"}, 20.0), hp.EVENT_SETTLE)

    def test_opening_removed_from_the_configuration_is_forgotten(self):
        tr = self.tracker()
        t0 = T(12)
        tr.update(t0, {self.A: "on"}, 21.5)
        tr.update(t0 + 10, {self.B: "off"}, 21.5)
        self.assertEqual(tr.open_openings(), [])

    def test_open_openings_lists_what_is_on(self):
        tr = self.tracker()
        tr.update(T(12), {self.A: "on", self.B: "off"}, 21.5)
        self.assertEqual(tr.open_openings(), [self.A])

    def test_state_survives_a_restart_and_resumes_the_settle(self):
        tr = self.tracker()
        t0 = T(12)
        self.drive(tr, t0, [(0, {self.A: "on"}, 21.5), (120, {self.A: "on"}, 21.5)])
        back = hp.WindowTracker(hp.WindowConfig(), tr.snapshot())
        self.assertEqual(back.state, "open")
        self.assertEqual(back.update(t0 + 900, {self.A: "off"}, 20.0), hp.EVENT_SETTLE)
        self.assertEqual(back.pre_open_air, 21.5)

    def test_a_settle_that_was_saved_is_resumed_with_its_deadline(self):
        tr = self.tracker()
        t0 = T(12)
        self.drive(
            tr,
            t0,
            [(0, {self.A: "on"}, 21.5), (120, {self.A: "on"}, 21.5), (3 * 3600, {self.A: "off"}, 18.0)],
        )
        saved = json.loads(json.dumps(tr.snapshot()))
        back = hp.WindowTracker(hp.WindowConfig(), saved)
        self.assertEqual(back.state, "settle")
        self.assertEqual(back.deadline(), tr.deadline())
        self.assertEqual(back.deadline(), t0 + 3 * 3600 + 3 * 3600)
        self.assertIsNone(back.update(back.deadline() - 1, {self.A: "off"}, 18.0))
        self.assertEqual(back.update(back.deadline(), {self.A: "off"}, 18.0), hp.EVENT_RESTORED)

    def test_garbage_saved_state_means_closed(self):
        tr = hp.WindowTracker(hp.WindowConfig(), {"state": "bogus", "opened_at": 5})
        self.assertEqual(tr.state, "closed")


class AiringLockoutTests(unittest.TestCase):
    """How long heat stays locked out after an opening: lockout_per_open_hour times the open time,
    from the close, at least settle_max_minutes and at most lockout_max_hours."""

    A = "binary_sensor.a"
    COLD = 17.0  # never within settle_recovered_c of the air before the opening

    def aired(self, hours, cfg=None, pre=21.5):
        """A tracker that was open for `hours` from T(12) and has just closed (settling)."""
        tr = hp.WindowTracker(cfg or hp.WindowConfig())
        t0 = T(12)
        tr.update(t0, {self.A: "on"}, pre)
        tr.update(t0 + 120, {self.A: "on"}, pre)
        closed = t0 + hours * 3600
        self.assertEqual(tr.update(closed, {self.A: "off"}, self.COLD), hp.EVENT_SETTLE)
        return tr, closed

    def lockout_minutes(self, hours, **kw):
        tr, closed = self.aired(hours, **kw)
        return (tr.deadline() - closed) / 60.0

    def test_the_lockout_is_the_open_time_between_an_hour_and_four_hours(self):
        self.assertEqual(self.lockout_minutes(20 / 60), 60)
        self.assertEqual(self.lockout_minutes(1), 60)
        self.assertEqual(self.lockout_minutes(2), 2 * 60)
        self.assertEqual(self.lockout_minutes(3), 3 * 60)
        self.assertEqual(self.lockout_minutes(6), 4 * 60)
        self.assertEqual(self.lockout_minutes(12), 4 * 60)

    def test_a_factor_of_zero_waits_the_minimum_whatever_the_open_time(self):
        cfg = hp.WindowConfig(lockout_per_open_hour=0.0)
        for hours in (20 / 60, 3, 6):
            self.assertEqual(self.lockout_minutes(hours, cfg=cfg), 60, hours)

    def test_the_settings_move_the_floor_and_the_ceiling(self):
        cfg = hp.WindowConfig(settle_max_minutes=30, lockout_max_hours=2)
        self.assertEqual(self.lockout_minutes(5 / 60, cfg=cfg), 30)
        self.assertEqual(self.lockout_minutes(1.5, cfg=cfg), 90)
        self.assertEqual(self.lockout_minutes(3, cfg=cfg), 2 * 60)
        cfg = hp.WindowConfig(lockout_per_open_hour=2.0)
        self.assertEqual(self.lockout_minutes(1.5, cfg=cfg), 3 * 60)

    def test_the_lockout_ends_at_the_deadline_and_not_a_second_before(self):
        for hours, minutes in ((20 / 60, 60), (3, 180), (6, 240)):
            with self.subTest(hours=hours):
                tr, closed = self.aired(hours)
                end = closed + minutes * 60
                self.assertEqual(tr.deadline(), end)
                self.assertIsNone(tr.update(end - 1, {self.A: "off"}, self.COLD))
                self.assertEqual(tr.state, "settle")
                self.assertEqual(tr.update(end, {self.A: "off"}, self.COLD), hp.EVENT_RESTORED)
                self.assertEqual(tr.state, "closed")
                self.assertIsNone(tr.deadline())

    def test_the_lockout_ends_early_when_the_air_is_back_within_three_tenths(self):
        tr, closed = self.aired(3, pre=21.5)
        self.assertIsNone(tr.update(closed + 600, {self.A: "off"}, 21.1))
        self.assertEqual(tr.update(closed + 1200, {self.A: "off"}, 21.2), hp.EVENT_RESTORED)
        self.assertEqual(tr.state, "closed")

    def test_a_long_airing_that_is_back_at_once_is_not_held_for_the_deadline(self):
        tr, closed = self.aired(6, pre=21.5)
        self.assertEqual(tr.update(closed + 60, {self.A: "off"}, 21.5), hp.EVENT_RESTORED)

    def test_the_lockout_is_counted_from_the_close_and_not_from_the_opening(self):
        tr, closed = self.aired(3)
        self.assertEqual(tr.settle_since, closed)
        self.assertEqual(tr.deadline(), closed + 3 * 3600)

    def test_reopening_keeps_the_first_opening_and_the_deadline_only_moves_later(self):
        tr, closed = self.aired(3)
        first = tr.deadline()
        reopen = closed + 600
        tr.update(reopen, {self.A: "on"}, 18.0)
        self.assertEqual(tr.update(reopen + 120, {self.A: "on"}, 18.0), hp.EVENT_OPENED)
        self.assertEqual(tr.opened_at, T(12))
        self.assertEqual(tr.pre_open_air, 21.5)
        self.assertIsNone(tr.deadline())
        again = reopen + 300
        self.assertEqual(tr.update(again, {self.A: "off"}, self.COLD), hp.EVENT_SETTLE)
        self.assertGreater(tr.deadline(), first)
        self.assertEqual(tr.deadline(), again + (again - T(12)))

    def test_a_reopening_after_a_short_airing_never_shortens_the_wait(self):
        tr, closed = self.aired(20 / 60)
        first = tr.deadline()
        tr.update(closed + 60, {self.A: "on"}, 19.0)
        tr.update(closed + 180, {self.A: "on"}, 19.0)
        tr.update(closed + 200, {self.A: "off"}, self.COLD)
        self.assertGreaterEqual(tr.deadline(), first)

    def test_the_deadline_exists_only_while_settling(self):
        tr = hp.WindowTracker(hp.WindowConfig())
        self.assertIsNone(tr.deadline())
        tr.update(T(12), {self.A: "on"}, 21.5)
        tr.update(T(12, 2), {self.A: "on"}, 21.5)
        self.assertEqual(tr.state, "open")
        self.assertIsNone(tr.deadline())

    def test_the_deadline_comes_from_the_snapshot_when_the_opening_time_was_lost(self):
        tr = hp.WindowTracker(
            hp.WindowConfig(),
            {"state": "settle", "settle_since": T(12), "opened_at": None, "pre_open_air": 21.5},
        )
        self.assertEqual(tr.deadline(), T(13))

    def test_a_snapshot_from_before_the_lockout_loads_and_times_out(self):
        saved = {"state": "settle", "opened_at": T(11), "pre_open_air": 21.5, "settle_since": T(12)}
        tr = hp.WindowTracker(hp.WindowConfig(), saved)
        self.assertEqual(tr.deadline(), T(13))
        self.assertEqual(tr.update(T(13), {}, 18.0), hp.EVENT_RESTORED)

    def test_the_snapshot_round_trip_keeps_every_field(self):
        tr, _closed = self.aired(3)
        saved = json.loads(json.dumps(tr.snapshot()))
        self.assertEqual(
            set(saved), {"state", "opened_at", "pre_open_air", "settle_since"}
        )
        back = hp.WindowTracker(hp.WindowConfig(), saved)
        self.assertEqual(back.snapshot(), tr.snapshot())
        self.assertEqual(back.deadline(), tr.deadline())


class HeatInTransit(unittest.TestCase):
    cfg = hp.TransitConfig(
        loops=(
            hp.TransitLoop("zone_1", "sensor.a", 2.0),
            hp.TransitLoop("zone_4", "sensor.b", 1.0),
        ),
        tau_minutes=40.0,
        k_per_kwh=0.2,
    )

    @staticmethod
    def calling(on=True, since=T(12)):
        return hp.CallState({"on": on, "since": since, "high": 23.5 if on else None})

    def test_water_flows_three_minutes_into_a_call_while_the_pump_runs(self):
        state = self.calling()
        self.assertFalse(hp.loops_flowing(state, 25.0, T(12) + 179))
        self.assertTrue(hp.loops_flowing(state, 25.0, T(12) + 180))
        self.assertTrue(hp.loops_flowing(state, 25.0, T(13)))

    def test_water_does_not_flow_with_the_pump_at_rest_or_unread(self):
        state = self.calling()
        self.assertFalse(hp.loops_flowing(state, 15.0, T(12, 30)))
        self.assertTrue(hp.loops_flowing(state, 15.1, T(12, 30)))
        self.assertFalse(hp.loops_flowing(state, 1.6, T(12, 30)))
        self.assertFalse(hp.loops_flowing(state, None, T(12, 30)))

    def test_water_does_not_flow_for_a_zone_that_is_not_calling(self):
        self.assertFalse(hp.loops_flowing(self.calling(on=False), 25.0, T(12, 30)))
        self.assertFalse(hp.loops_flowing(hp.CallState(), 25.0, T(12, 30)))
        self.assertFalse(hp.loops_flowing(hp.CallState({"on": True}), 25.0, T(12, 30)))

    def test_a_loop_gives_off_its_flow_times_the_drop(self):
        loop = self.cfg.loops[0]
        self.assertAlmostEqual(hp.loop_power_w(loop, 36.0, 33.0), 2.0 * 69.8 * 3.0)
        self.assertAlmostEqual(hp.loop_power_w(self.cfg.loops[1], 35.5, 33.0), 1.0 * 69.8 * 2.5)
        self.assertGreater(hp.loop_power_w(loop, 36.0, 21.01), 0.0)
        self.assertGreater(hp.loop_power_w(loop, 36.0, 35.99), 0.0)

    def test_a_loop_with_a_reading_missing_or_off_gives_nothing(self):
        loop = self.cfg.loops[0]
        nan = float("nan")
        for supply, returned in (
            (None, 33.0), (36.0, None), (None, None), (36.0, 36.0), (36.0, 37.0), (36.0, 21.0),
            (36.0, 20.0), (nan, 30.0), (36.0, nan),
        ):
            self.assertEqual(hp.loop_power_w(loop, supply, returned), 0.0, (supply, returned))

    def test_the_heat_grows_by_the_power_over_the_step(self):
        state = hp.TransitState()
        state.advance(self.cfg, 0.0, T(12))
        state.advance(self.cfg, 3600.0, T(12) + 600)
        self.assertAlmostEqual(state.q_kwh, 0.6)
        self.assertEqual(state.ts, T(12) + 600)

    def test_the_heat_leaves_with_tau_also_while_nothing_flows(self):
        state = hp.TransitState({"q_kwh": 1.0, "ts": T(12)})
        for step in range(1, 5):
            state.advance(self.cfg, 0.0, T(12) + step * 600)
        self.assertAlmostEqual(state.q_kwh, math.exp(-1.0))

    def test_the_heat_adds_what_the_step_brings_after_what_is_left(self):
        state = hp.TransitState({"q_kwh": 1.0, "ts": T(12)})
        state.advance(self.cfg, 1800.0, T(12) + 600)
        self.assertAlmostEqual(state.q_kwh, math.exp(-600 / 2400) + 0.3)

    def test_a_step_is_at_most_fifteen_minutes(self):
        state = hp.TransitState({"q_kwh": 0.0, "ts": T(12)})
        state.advance(self.cfg, 3600.0, T(15))
        self.assertAlmostEqual(state.q_kwh, 0.9)
        state = hp.TransitState({"q_kwh": 1.0, "ts": T(12)})
        state.advance(self.cfg, 0.0, T(15))
        self.assertAlmostEqual(state.q_kwh, math.exp(-900 / 2400))

    def test_the_first_step_after_a_lost_state_has_no_length(self):
        state = hp.TransitState()
        state.advance(self.cfg, 5000.0, T(12))
        self.assertEqual((state.q_kwh, state.ts), (0.0, T(12)))
        state.advance(self.cfg, 3600.0, T(12) + 600)
        self.assertAlmostEqual(state.q_kwh, 0.6)

    def test_a_clock_that_goes_back_adds_nothing(self):
        state = hp.TransitState({"q_kwh": 0.5, "ts": T(12)})
        state.advance(self.cfg, 5000.0, T(12) - 300)
        self.assertEqual((state.q_kwh, state.ts), (0.5, T(12) - 300))

    def test_the_coast_is_k_per_kwh_of_the_heat_up_to_half_a_degree(self):
        for q_kwh, expected in ((0.0, 0.0), (0.5, 0.1), (2.0, 0.4), (2.5, 0.5), (10.0, 0.5)):
            state = hp.TransitState({"q_kwh": q_kwh, "ts": T(12)})
            self.assertAlmostEqual(state.coast_c(self.cfg), expected, msg=q_kwh)
        hot = hp.TransitConfig(self.cfg.loops, 20.0, 3.2)
        self.assertEqual(hp.TransitState({"q_kwh": 0.3, "ts": T(12)}).coast_c(hot), 0.5)

    def test_the_state_survives_a_json_round_trip(self):
        state = hp.TransitState()
        state.advance(self.cfg, 0.0, T(12))
        state.advance(self.cfg, 2000.0, T(12) + 300)
        again = hp.TransitState(json.loads(json.dumps(state.snapshot())))
        self.assertEqual(again.snapshot(), state.snapshot())
        self.assertEqual(set(state.snapshot()), {"q_kwh", "ts"})

    def test_junk_in_the_saved_state_is_a_lost_state(self):
        for junk in (
            None, [], "x", 5, {"q_kwh": "x", "ts": "y"}, {"q_kwh": True, "ts": True},
            {"q_kwh": float("nan"), "ts": float("inf")}, {"q_kwh": -1.0, "ts": None},
        ):
            state = hp.TransitState(junk)
            self.assertEqual((state.q_kwh, state.ts), (0.0, None), junk)
        state = hp.TransitState({"q_kwh": 5, "ts": 7})
        self.assertEqual((state.q_kwh, state.ts), (5.0, 7.0))


class StartWindow(unittest.TestCase):
    cfg = hp.CallConfig()

    def test_a_start_is_taken_in_the_first_six_minutes_of_each_half_hour(self):
        for hour, minute, expected in (
            (12, 0, True), (12, 5, True), (12, 6, False), (12, 29, False),
            (12, 30, True), (12, 35, True), (12, 36, False), (13, 0, True),
        ):
            self.assertEqual(hp.in_start_window(self.cfg, T(hour, minute)), expected, (hour, minute))
        self.assertTrue(hp.in_start_window(self.cfg, T(12, 5) + 59))
        self.assertFalse(hp.in_start_window(self.cfg, T(12, 6)))

    def test_the_slot_and_the_window_follow_the_config(self):
        cfg = hp.CallConfig(start_every_minutes=60, start_window_minutes=10)
        self.assertTrue(hp.in_start_window(cfg, T(12, 9)))
        self.assertFalse(hp.in_start_window(cfg, T(12, 10)))
        self.assertFalse(hp.in_start_window(cfg, T(12, 30)))
        self.assertTrue(hp.in_start_window(cfg, T(13, 0)))


class DecideCall(unittest.TestCase):
    cfg = hp.CallConfig()
    AIM = 21.5

    def decide(
        self, air, now=T(12), on=False, since=None, allowed=True, aim=None, cfg=None, coast=0.0
    ):
        state = hp.CallState({"on": on, "since": since, "high": 23.5 if on else None})
        aim = self.AIM if aim is None else aim
        return hp.decide_call(cfg or self.cfg, state, aim, air, allowed, now, coast)

    def test_a_cold_room_starts_a_call_at_the_start_of_a_slot(self):
        self.assertEqual(self.decide(21.0, T(12)), (True, "start"))
        self.assertEqual(self.decide(21.0, T(12, 5)), (True, "start"))
        self.assertEqual(self.decide(21.0, T(12, 30)), (True, "start"))

    def test_outside_the_start_window_it_waits_for_the_next_slot(self):
        for now in (T(12, 6), T(12, 15), T(12, 29), T(12, 36)):
            self.assertEqual(self.decide(21.0, now), (False, "wait_slot"), hp.hhmm(now, TZ))

    def test_a_start_needs_the_air_more_than_two_tenths_under_the_aim(self):
        self.assertEqual(self.decide(21.3), (False, "air_ok"))
        self.assertEqual(self.decide(21.29), (True, "start"))
        self.assertEqual(self.decide(21.5), (False, "air_ok"))
        self.assertEqual(self.decide(23.0), (False, "air_ok"))

    def test_a_call_runs_while_the_air_is_at_most_two_tenths_over_the_aim(self):
        self.assertEqual(self.decide(20.0, on=True, since=T(11)), (True, "calling"))
        self.assertEqual(self.decide(21.7, on=True, since=T(11)), (True, "calling"))
        self.assertEqual(self.decide(21.71, on=True, since=T(11)), (False, "stop"))

    def test_a_call_lasts_at_least_twenty_minutes(self):
        self.assertEqual(self.decide(22.5, T(12, 1), on=True, since=T(12)), (True, "min_on"))
        self.assertEqual(self.decide(22.5, T(12, 19), on=True, since=T(12)), (True, "min_on"))
        self.assertEqual(self.decide(22.5, T(12, 20), on=True, since=T(12)), (False, "stop"))

    def test_the_pause_after_a_call_lasts_at_least_twenty_minutes(self):
        self.assertEqual(self.decide(21.0, T(12), since=T(11, 41)), (False, "min_off"))
        self.assertEqual(self.decide(21.0, T(12), since=T(11, 40)), (True, "start"))
        self.assertEqual(self.decide(21.0, T(12, 30), since=T(12, 20)), (False, "min_off"))
        self.assertEqual(self.decide(21.0, T(12, 30), since=T(12, 10)), (True, "start"))

    def test_a_pause_that_has_run_out_still_waits_for_the_slot(self):
        self.assertEqual(self.decide(21.0, T(12, 10), since=T(11, 0)), (False, "wait_slot"))

    def test_a_zone_that_has_never_called_may_start_at_once(self):
        self.assertEqual(self.decide(21.0, T(12), since=None), (True, "start"))

    def test_heat_that_is_not_allowed_stops_a_call_at_once(self):
        self.assertEqual(
            self.decide(20.0, T(12, 1), on=True, since=T(12), allowed=False), (False, "not_allowed")
        )

    def test_heat_that_is_not_allowed_never_starts_a_call(self):
        self.assertEqual(self.decide(15.0, T(12), allowed=False), (False, "not_allowed"))

    def test_without_room_air_there_is_no_call(self):
        self.assertEqual(self.decide(None), (False, "no_air"))
        self.assertEqual(self.decide(None, on=True, since=T(11)), (False, "no_air"))
        self.assertEqual(self.decide(None, allowed=False), (False, "not_allowed"))

    def test_the_decision_leaves_the_state_alone(self):
        state = hp.CallState({"on": False, "since": T(11)})
        hp.decide_call(self.cfg, state, 21.5, 20.0, True, T(12))
        self.assertEqual(state.snapshot(), {"on": False, "since": T(11), "high": None})

    def test_an_air_hovering_around_the_aim_neither_starts_nor_stops_a_call(self):
        for air in (21.4, 21.45, 21.5, 21.6, 21.7, 21.35):
            self.assertEqual(self.decide(air, T(12))[0], False, air)
        for air in (21.35, 21.5, 21.65, 21.7):
            self.assertEqual(self.decide(air, T(13), on=True, since=T(12))[0], True, air)

    def test_the_rule_follows_the_config(self):
        cfg = hp.CallConfig(hysteresis_c=0.5, min_on_minutes=5, min_off_minutes=0)
        self.assertEqual(self.decide(21.01, cfg=cfg), (False, "air_ok"))
        self.assertEqual(self.decide(20.99, cfg=cfg), (True, "start"))
        self.assertEqual(self.decide(22.0, T(12, 5), on=True, since=T(12), cfg=cfg), (True, "calling"))
        self.assertEqual(self.decide(22.1, T(12, 4), on=True, since=T(12), cfg=cfg), (True, "min_on"))
        self.assertEqual(self.decide(22.1, T(12, 5), on=True, since=T(12), cfg=cfg), (False, "stop"))
        self.assertEqual(self.decide(20.0, T(12), since=T(11, 59), cfg=cfg), (True, "start"))

    def test_the_aim_decides_not_the_old_setpoint(self):
        self.assertEqual(self.decide(21.0, aim=20.5), (False, "air_ok"))
        self.assertEqual(self.decide(21.0, aim=21.5), (True, "start"))

    def test_a_coast_stops_the_call_once_the_air_plus_the_coast_is_a_tenth_over_the_aim(self):
        self.assertEqual(self.decide(21.39, on=True, since=T(11), coast=0.2), (True, "calling"))
        self.assertEqual(self.decide(21.41, on=True, since=T(11), coast=0.2), (False, "stop"))
        self.assertEqual(self.decide(21.51, on=True, since=T(11), coast=0.05), (True, "calling"))
        self.assertEqual(self.decide(21.56, on=True, since=T(11), coast=0.05), (False, "stop"))
        self.assertEqual(self.decide(21.0, on=True, since=T(11), coast=0.5), (True, "calling"))
        self.assertEqual(self.decide(21.2, on=True, since=T(11), coast=0.5), (False, "stop"))

    def test_without_a_coast_the_call_keeps_running_to_two_tenths_over_the_aim(self):
        for coast in (0.0, -0.1):
            running = dict(on=True, since=T(11), coast=coast)
            self.assertEqual(self.decide(21.65, **running), (True, "calling"))
            self.assertEqual(self.decide(21.69, **running), (True, "calling"))
            self.assertEqual(self.decide(21.71, **running), (False, "stop"))

    def test_a_coast_leaves_a_call_its_minimum_time(self):
        running = dict(on=True, since=T(12), coast=0.3)
        self.assertEqual(self.decide(22.0, T(12, 1), **running), (True, "min_on"))
        self.assertEqual(self.decide(22.0, T(12, 19), **running), (True, "min_on"))
        self.assertEqual(self.decide(22.0, T(12, 20), **running), (False, "stop"))

    def test_a_coast_does_not_change_when_a_call_starts_or_when_heat_is_refused(self):
        for coast in (0.0, 0.3, 0.5):
            with self.subTest(coast=coast):
                self.assertEqual(self.decide(21.29, coast=coast), (True, "start"))
                self.assertEqual(self.decide(21.3, coast=coast), (False, "air_ok"))
                self.assertEqual(self.decide(21.0, T(12, 10), coast=coast), (False, "wait_slot"))
                resting = self.decide(21.0, since=T(11, 50), coast=coast)
                self.assertEqual(resting, (False, "min_off"))
                self.assertEqual(
                    self.decide(20.0, T(12, 1), on=True, since=T(12), allowed=False, coast=coast),
                    (False, "not_allowed"),
                )
                blind = self.decide(None, on=True, since=T(11), coast=coast)
                self.assertEqual(blind, (False, "no_air"))

    def test_a_call_is_kept_while_the_coast_is_still_short_of_the_aim(self):
        self.assertEqual(self.decide(20.9, on=True, since=T(11), coast=0.3), (True, "calling"))

    def test_a_slot_start_through_a_whole_cycle(self):
        state = hp.CallState()
        log = []
        air = 21.0
        for step in range(0, 6 * 12):
            now = T(12) + step * 300
            on, reason = hp.decide_call(self.cfg, state, 21.5, air, True, now)
            if state.apply(on, now, 23.5):
                log.append((hp.hhmm(now, TZ), "on" if on else "off", reason))
            air += 0.125 if state.on else -0.0625
        self.assertEqual(
            log,
            [
                ("12:00", "on", "start"),
                ("12:30", "off", "stop"),
                ("13:30", "on", "start"),
                ("14:00", "off", "stop"),
                ("15:00", "on", "start"),
                ("15:30", "off", "stop"),
                ("16:30", "on", "start"),
                ("17:00", "off", "stop"),
            ],
        )


class CallStateTests(unittest.TestCase):
    def test_a_new_state_is_not_calling(self):
        s = hp.CallState()
        self.assertEqual((s.on, s.since, s.high), (False, None, None))
        self.assertEqual(s.snapshot(), {"on": False, "since": None, "high": None})

    def test_apply_reports_a_start_and_a_stop_and_nothing_else(self):
        s = hp.CallState()
        self.assertFalse(s.apply(False, T(12)))
        self.assertIsNone(s.since)
        self.assertTrue(s.apply(True, T(12), 23.5))
        self.assertEqual((s.on, s.since, s.high), (True, T(12), 23.5))
        self.assertFalse(s.apply(True, T(12, 10), 24.0))
        self.assertEqual((s.since, s.high), (T(12), 23.5))
        self.assertTrue(s.apply(False, T(12, 30), 24.0))
        self.assertEqual((s.on, s.since, s.high), (False, T(12, 30), None))

    def test_a_call_without_a_stored_high_setpoint_takes_the_next_one_offered(self):
        s = hp.CallState({"on": True, "since": T(12)})
        self.assertIsNone(s.high)
        self.assertFalse(s.apply(True, T(12, 5), 23.5))
        self.assertEqual((s.since, s.high), (T(12), 23.5))

    def test_the_state_survives_a_json_round_trip(self):
        s = hp.CallState()
        s.apply(True, T(12), 23.5)
        again = hp.CallState(json.loads(json.dumps(s.snapshot())))
        self.assertEqual(again.snapshot(), s.snapshot())
        s.apply(False, T(12, 30))
        again = hp.CallState(json.loads(json.dumps(s.snapshot())))
        self.assertEqual(again.snapshot(), {"on": False, "since": T(12, 30), "high": None})

    def test_junk_in_the_saved_state_is_ignored(self):
        for junk in (None, [], "on", 5, {"on": "yes"}, {"on": 1}, {"since": "x"}, {"high": "x"}):
            s = hp.CallState(junk)
            self.assertEqual((s.on, s.since, s.high), (False, None, None), junk)
        s = hp.CallState({"on": True, "since": True, "high": float("nan")})
        self.assertEqual((s.on, s.since, s.high), (True, None, None))
        s = hp.CallState({"on": True, "since": float("inf"), "high": False})
        self.assertEqual((s.on, s.since, s.high), (True, None, None))

    def test_a_high_setpoint_is_only_kept_for_a_call_that_is_on(self):
        self.assertIsNone(hp.CallState({"on": False, "since": T(12), "high": 23.5}).high)
        self.assertEqual(hp.CallState({"on": True, "since": T(12), "high": 23.5}).high, 23.5)

    def test_whole_numbers_come_back_as_floats(self):
        s = hp.CallState({"on": True, "since": 5, "high": 24})
        self.assertEqual((s.since, s.high), (5.0, 24.0))
        self.assertIsInstance(s.since, float)
        self.assertIsInstance(s.high, float)


class HighLimitAndDrive(unittest.TestCase):
    cfg = hp.CallConfig()

    def test_the_high_setpoint_is_two_degrees_over_the_aim_rounded_up_to_the_step(self):
        for aim, expected in ((21.5, 23.5), (21.6, 24.0), (21.0, 23.0), (20.3, 22.5), (19.0, 21.0)):
            self.assertEqual(hp.high_limit(self.cfg, aim, 25.0), expected, aim)

    def test_the_high_setpoint_stops_at_the_device_upper_limit(self):
        self.assertEqual(hp.high_limit(self.cfg, 22.5, 24.0), 24.0)
        self.assertEqual(hp.high_limit(self.cfg, 24.0, 25.0), 25.0)
        self.assertEqual(hp.high_limit(self.cfg, 23.0, 24.0), 24.0)

    def test_the_margin_follows_the_config(self):
        self.assertEqual(hp.high_limit(hp.CallConfig(high_margin_c=1.0), 21.5, 24.0), 22.5)
        self.assertEqual(hp.high_limit(hp.CallConfig(high_margin_c=0.0), 21.3, 24.0), 21.5)

    def calling(self, high=23.5):
        s = hp.CallState()
        s.apply(True, T(12), high)
        return s

    def test_a_zone_that_is_not_calling_sits_at_the_device_minimum(self):
        self.assertEqual(hp.drive_setpoint(hp.CallState(), 21.5, False, 5.0, 24.0), 5.0)
        self.assertEqual(hp.drive_setpoint(hp.CallState(), 24.0, False, 5.0, 24.0), 5.0)

    def test_a_call_is_driven_at_its_fixed_high_setpoint_whatever_the_aim_does(self):
        s = self.calling(23.5)
        for aim in (21.5, 22.0, 19.0):
            self.assertEqual(hp.drive_setpoint(s, aim, False, 5.0, 24.0), 23.5, aim)

    def test_the_drive_stays_inside_what_the_device_takes(self):
        self.assertEqual(hp.drive_setpoint(self.calling(26.0), 21.5, False, 5.0, 24.0), 24.0)
        self.assertEqual(hp.drive_setpoint(self.calling(3.0), 21.5, False, 5.0, 24.0), 5.0)
        self.assertEqual(hp.drive_setpoint(hp.CallState(), 21.5, False, 7.0, 24.0), 7.0)

    def test_a_call_that_lost_its_high_setpoint_falls_back_to_the_minimum(self):
        s = hp.CallState({"on": True, "since": T(12)})
        self.assertEqual(hp.drive_setpoint(s, 21.5, False, 5.0, 24.0), 5.0)

    def test_without_room_air_the_aim_goes_out_as_an_ordinary_setpoint(self):
        self.assertEqual(hp.drive_setpoint(hp.CallState(), 21.3, True, 5.0, 24.0), 21.5)
        self.assertEqual(hp.drive_setpoint(hp.CallState(), 21.2, True, 5.0, 24.0), 21.0)
        self.assertEqual(hp.drive_setpoint(self.calling(23.5), 21.5, True, 5.0, 24.0), 21.5)
        self.assertEqual(hp.drive_setpoint(hp.CallState(), 30.0, True, 5.0, 24.0), 24.0)
        self.assertEqual(hp.drive_setpoint(hp.CallState(), 3.0, True, 5.0, 24.0), 5.0)


class TokenBucketTests(unittest.TestCase):
    def test_four_writes_an_hour_give_a_token_every_quarter_hour(self):
        b = hp.TokenBucket(4)
        for _ in range(4):
            self.assertTrue(b.take(T(12)))
        self.assertFalse(b.take(T(12, 14)))
        self.assertTrue(b.take(T(12, 15)))
        self.assertFalse(b.take(T(12, 16)))
        self.assertTrue(b.take(T(12, 30)))

    def test_two_writes_then_empty(self):
        b = hp.TokenBucket(2)
        self.assertTrue(b.take(T(12)))
        self.assertTrue(b.take(T(12)))
        self.assertFalse(b.take(T(12)))

    def test_one_token_back_every_half_hour(self):
        b = hp.TokenBucket(2)
        b.take(T(12))
        b.take(T(12))
        self.assertFalse(b.take(T(12, 29)))
        self.assertTrue(b.take(T(12, 30)))
        self.assertFalse(b.take(T(12, 31)))
        self.assertTrue(b.take(T(13)))

    def test_the_bucket_never_holds_more_than_its_capacity(self):
        b = hp.TokenBucket(2)
        self.assertEqual(b.available(T(12)), 2.0)
        self.assertEqual(b.available(T(20)), 2.0)

    def test_state_round_trips(self):
        b = hp.TokenBucket(2)
        b.take(T(12))
        b.take(T(12))
        again = hp.TokenBucket(2, b.snapshot())
        self.assertFalse(again.take(T(12, 10)))
        self.assertTrue(again.take(T(12, 30)))


class WriteGateTests(unittest.TestCase):
    def gate(self, desired, last, now, since, priority=None, bucket=None):
        return hp.write_gate(desired, last, now, since, priority, bucket or hp.TokenBucket(2), 600)

    def test_no_change_no_write(self):
        self.assertEqual(self.gate(22.0, 22.0, T(12), T(10)), hp.GateResult(False, "unchanged"))

    def test_a_new_value_must_be_stable_for_ten_minutes(self):
        self.assertEqual(self.gate(22.5, 22.0, T(13, 9), T(13)), hp.GateResult(False, "settling"))
        self.assertEqual(self.gate(22.5, 22.0, T(13, 10), T(13)), hp.GateResult(True, "stable"))

    def test_a_priority_event_writes_at_once(self):
        self.assertEqual(
            self.gate(19.0, 22.0, T(13, 1), T(13), "window_open"),
            hp.GateResult(True, "priority:window_open"),
        )

    def test_a_missing_token_defers_even_a_priority(self):
        bucket = hp.TokenBucket(2)
        bucket.take(T(13))
        bucket.take(T(13))
        self.assertEqual(
            self.gate(19.0, 22.0, T(13, 1), T(13), "window_open", bucket),
            hp.GateResult(False, "no_token"),
        )
        self.assertEqual(
            self.gate(22.5, 22.0, T(13, 12), T(13), None, bucket), hp.GateResult(False, "no_token")
        )

    def test_the_first_value_is_written_once_stable(self):
        self.assertTrue(self.gate(22.0, None, T(13, 10), T(13)).write)

    def test_a_deferred_write_does_not_use_a_token(self):
        bucket = hp.TokenBucket(2)
        self.gate(22.5, 22.0, T(13, 1), T(13), None, bucket)
        self.assertEqual(bucket.available(T(13, 1)), 2.0)

    def test_the_reported_value_decides_if_a_live_write_is_needed(self):
        self.assertEqual(self.gate(22.0, 22.0, T(12), T(10)), hp.GateResult(False, "unchanged"))
        self.assertEqual(self.gate(22.0, 20.0, T(12), T(10)), hp.GateResult(True, "stable"))
        self.assertEqual(self.gate(22.0, None, T(12), T(10)), hp.GateResult(True, "stable"))

    def test_a_pending_write_blocks_even_a_priority_without_spending_a_token(self):
        bucket = hp.TokenBucket(2)
        result = hp.write_gate(19.0, 22.0, T(12), T(10), "window_open", bucket, 600, pending=True)
        self.assertEqual(result, hp.GateResult(False, "pending"))
        self.assertEqual(bucket.available(T(12)), 2.0)

    def test_clearing_pending_allows_the_next_eligible_write(self):
        bucket = hp.TokenBucket(2)
        hp.write_gate(22.0, 20.0, T(12), T(10), None, bucket, 600, pending=True)
        result = hp.write_gate(22.0, 20.0, T(12), T(10), None, bucket, 600, pending=False)
        self.assertEqual(result, hp.GateResult(True, "stable"))
        self.assertEqual(bucket.available(T(12)), 1.0)


class CallDayReplay(unittest.TestCase):
    """A day of one zone through plan, call rule, drive and write gate in five-minute ticks, with a
    room that cools while the heat is off and warms while the zone calls."""

    TICK = 300

    def replay(self, zone="family", air=21.0, cool=0.02, warm=0.06, **kw):
        zc = CFG.zones[zone]
        state = hp.CallState()
        bucket = hp.TokenBucket(CFG.writes_per_hour)
        last, desired, since, priority = hp.DEVICE_MIN_C, None, T(0), None
        ticks, writes = [], []
        for step in range(24 * 12):
            now = T(0) + step * self.TICK
            p = hp.plan(zc, inputs(zone, **kw), now, TZ)
            on, _reason = hp.decide_call(CFG.call, state, p.aim, air, p.allowed, now)
            if state.apply(on, now, hp.high_limit(CFG.call, p.aim, zc.upper_c)):
                priority = "call"
            drive = hp.drive_setpoint(state, p.aim, False, hp.DEVICE_MIN_C, zc.upper_c)
            if drive != desired:
                desired, since = drive, now
            gate = hp.write_gate(desired, last, now, since, priority, bucket, 600)
            if gate.write:
                last, priority = desired, None
                writes.append((now, desired))
            ticks.append((now, state.on, air, p.aim))
            air += warm if state.on else -cool
        return ticks, writes

    def runs(self, ticks):
        """(start, end) of every call that began and ended inside the day."""
        out, start = [], None
        for ts, on, _air, _aim in ticks:
            if on and start is None:
                start = ts
            elif not on and start is not None:
                out.append((start, ts))
                start = None
        return out

    def test_every_call_starts_in_a_start_window_and_lasts_at_least_twenty_minutes(self):
        ticks, _writes = self.replay()
        runs = self.runs(ticks)
        self.assertGreaterEqual(len(runs), 3)
        for start, end in runs:
            self.assertTrue(hp.in_start_window(CFG.call, start), hp.hhmm(start, TZ))
            self.assertGreaterEqual(end - start, 20 * 60, hp.hhmm(start, TZ))

    def test_the_pause_between_calls_is_at_least_twenty_minutes(self):
        ticks, _writes = self.replay()
        runs = self.runs(ticks)
        for (_s1, end), (start, _e2) in zip(runs, runs[1:]):
            self.assertGreaterEqual(start - end, 20 * 60, hp.hhmm(end, TZ))

    def test_the_air_stays_near_the_aim_while_the_aim_is_flat(self):
        ticks, _writes = self.replay()
        flat = [air - aim for ts, _on, air, aim in ticks if T(7) <= ts < T(13)]
        self.assertLess(max(flat), 0.4)
        self.assertGreater(min(flat), -0.4)

    def test_the_thermostat_is_written_at_the_minimum_or_at_a_high_setpoint_above_the_aim(self):
        _ticks, writes = self.replay()
        self.assertGreaterEqual(len(writes), 6)
        values = [value for _ts, value in writes]
        for value in values:
            self.assertTrue(value == hp.DEVICE_MIN_C or 22.5 <= value <= 24.0, value)
        for a, b in zip(values, values[1:]):
            self.assertNotEqual(a, b)

    def test_a_call_is_written_in_the_tick_it_starts_and_stops(self):
        ticks, writes = self.replay()
        written = {ts for ts, _value in writes}
        for start, end in self.runs(ticks):
            self.assertIn(start, written, hp.hhmm(start, TZ))
            self.assertIn(end, written, hp.hhmm(end, TZ))

    def test_a_day_with_the_window_open_never_calls_nor_writes_high(self):
        ticks, writes = self.replay(window_state=hp.STATE_OPEN)
        self.assertFalse(any(on for _ts, on, _air, _aim in ticks))
        self.assertEqual(writes, [])

    def test_a_cold_room_that_is_not_allowed_heat_is_left_alone_until_it_is(self):
        ticks, _writes = self.replay(air=18.0, cool=0.0, warm=0.0, party_windows=((T(0), T(6)),))
        first_on = min(ts for ts, on, _air, _aim in ticks if on)
        self.assertEqual(first_on, T(6))


class ProjectBlocks(unittest.TestCase):
    def test_project_blocks_gives_the_aim_of_the_day(self):
        zc = CFG.zones["family"]
        blocks = hp.project_blocks(zc, inputs(), T(0), T(0, day=18), TZ)
        self.assertEqual(
            [(hp.hhmm(b[0], TZ), hp.hhmm(b[1], TZ), b[2], b[3]) for b in blocks],
            [
                ("00:00", "06:00", "hold", 21.0),
                ("06:00", "13:00", "hold", 21.5),
                ("13:00", "19:00", "hold", 22.0),
                ("19:00", "00:00", "hold", 21.0),
            ],
        )

    def test_block_edges_fall_on_round_times_whatever_the_start(self):
        zc = CFG.zones["family"]
        now = T(12, 3) + 27
        blocks = hp.project_blocks(zc, inputs(), now, T(0, day=18), TZ)
        self.assertEqual(blocks[0][0], now)
        self.assertEqual([hp.hhmm(b[1], TZ) for b in blocks][:2], ["13:00", "19:00"])
        self.assertEqual(blocks[0][1], T(13))

    def test_project_blocks_include_a_cut_and_ignore_an_open_window(self):
        zc = CFG.zones["family"]
        cut = ((T(1), T(14), "2026-10-17"),)
        blocks = hp.project_blocks(
            zc, inputs(cut_windows=cut, window_state="open"), T(0), T(0, day=18), TZ
        )
        modes = [(hp.hhmm(b[0], TZ), b[2], b[3]) for b in blocks]
        self.assertEqual(
            modes,
            [
                ("00:00", "hold", 21.0),
                ("01:00", "sun_cut", 20.5),
                ("14:00", "hold", 22.0),
                ("19:00", "hold", 21.0),
            ],
        )


class BlockLogTests(unittest.TestCase):
    def test_changes_close_blocks_and_the_day_keeps_its_history(self):
        log = hp.BlockLog()
        log.observe(T(0), "2026-10-17", "hold", 21.5)
        log.observe(T(0, 5), "2026-10-17", "hold", 21.5)
        log.observe(T(6, 10), "2026-10-17", "hold", 22.0)
        log.observe(T(13, 10), "2026-10-17", "hold", 22.5)
        self.assertEqual(
            log.closed, [[T(0), T(6, 10), "hold", 21.5], [T(6, 10), T(13, 10), "hold", 22.0]]
        )
        self.assertEqual(log.current, [T(13, 10), "hold", 22.5])

    def test_a_new_day_starts_clean(self):
        log = hp.BlockLog()
        log.observe(T(0), "2026-10-17", "hold", 21.5)
        log.observe(T(8), "2026-10-17", "hold", 22.0)
        log.observe(T(0, day=18), "2026-10-18", "hold", 21.5)
        self.assertEqual(log.closed, [])
        self.assertEqual(log.day, "2026-10-18")

    def test_state_survives_a_round_trip(self):
        log = hp.BlockLog()
        log.observe(T(0), "2026-10-17", "hold", 21.5)
        log.observe(T(8), "2026-10-17", "hold", 22.0)
        again = hp.BlockLog(log.snapshot())
        self.assertEqual(
            (again.day, again.closed, again.current), (log.day, log.closed, log.current)
        )

    def test_render_keeps_past_blocks_and_continues_with_the_projection(self):
        log = hp.BlockLog()
        log.observe(T(0), "2026-10-17", "hold", 21.5)
        log.observe(T(6, 10), "2026-10-17", "hold", 22.0)
        projected = [
            (T(8), T(13), "hold", 22.0),
            (T(13), T(19), "hold", 22.5),
            (T(19), T(0, day=18), "hold", 21.5),
        ]
        rows = hp.render_blocks(log, projected, T(8), T(0, day=18), TZ)
        self.assertEqual(
            rows,
            [
                {"start": "00:00", "end": "06:10", "mode": "hold", "setpoint": "21.5"},
                {"start": "06:10", "end": "13:00", "mode": "hold", "setpoint": "22.0"},
                {"start": "13:00", "end": "19:00", "mode": "hold", "setpoint": "22.5"},
                {"start": "19:00", "end": "24:00", "mode": "hold", "setpoint": "21.5"},
            ],
        )


def tick(
    ts=T(12),
    hour=12,
    plan_mode="hold",
    season_on=True,
    calling=False,
    air=21.5,
    target=21.5,
    lower=19.0,
    fallback=False,
    dt_s=300.0,
):
    return hp.TickSample(ts, hour, dt_s, plan_mode, season_on, calling, air, target, lower, fallback)


class ZoneDayTests(unittest.TestCase):
    def test_hours_per_mode_and_call_hours(self):
        d = hp.ZoneDay("2026-10-17")
        for _ in range(12):
            d.add_tick(tick(calling=True))
        for _ in range(6):
            d.add_tick(tick(plan_mode="sun_cut"))
        rec = d.record("family")
        self.assertEqual(rec["mode_hours"], {"hold": 1.0, "sun_cut": 0.5})
        self.assertEqual(rec["call_hours"], 1.0)
        self.assertEqual(rec["ticks"], 18)

    def test_comfort_band_counts_season_daytime_ticks_outside_coasting(self):
        d = hp.ZoneDay("2026-10-17")
        d.add_tick(tick(air=21.9))  # inside the band
        d.add_tick(tick(air=22.2))  # outside
        d.add_tick(tick(hour=3, air=25.0))  # night: not counted
        d.add_tick(tick(plan_mode="window", air=18.0))  # coasting: not counted
        d.add_tick(tick(season_on=False, air=25.0))  # off season: not counted
        rec = d.record("family")
        self.assertEqual((rec["band_ticks"], rec["in_band_pct"]), (2, 50.0))

    def test_air_range_and_below_lower_limit(self):
        d = hp.ZoneDay("2026-10-17")
        for air in (20.0, 18.5, 22.0, None):
            d.add_tick(tick(air=air))
        d.add_tick(tick(plan_mode="settle", air=17.0))
        rec = d.record("family")
        self.assertEqual((rec["air_min"], rec["air_max"]), (17.0, 22.0))
        self.assertEqual(rec["below_lower_ticks"], 1)
        self.assertEqual(rec["air_mean"], round((20.0 + 18.5 + 22.0 + 17.0) / 4, 2))

    def test_afternoon_excess_tracks_the_overheating_the_cut_aims_at(self):
        d = hp.ZoneDay("2026-10-17")
        d.add_tick(tick(hour=9, air=25.0, target=21.5))
        d.add_tick(tick(hour=14, air=22.4, target=21.5))
        d.add_tick(tick(hour=15, air=22.9, target=21.5))
        rec = d.record("family")
        self.assertEqual(rec["afternoon_excess_max"], 1.4)

    def test_fallback_hours(self):
        d = hp.ZoneDay("2026-10-17")
        d.add_tick(tick(fallback=True))
        d.add_tick(tick())
        self.assertEqual(d.record("z")["fallback_hours"], 0.08)

    def test_would_write_reverts_within_half_an_hour(self):
        d = hp.ZoneDay("2026-10-17")
        d.add_would_write(T(10), 22.0)
        d.add_would_write(T(10, 20), 21.5)
        d.add_would_write(T(10, 40), 22.0)  # back to 22.0 20 minutes after leaving it
        self.assertEqual(d.reverts, 1)
        d.add_would_write(T(12), 21.5)
        d.add_would_write(T(13), 22.0)  # a long time later: not a revert
        self.assertEqual(d.reverts, 1)
        self.assertEqual(len(d.would_writes), 5)

    def test_budget_is_noted_once_and_the_record_names_the_zone_and_the_day(self):
        d = hp.ZoneDay("2026-10-17")
        d.note_budget({"h_pred": 4.0})
        d.note_budget({"h_pred": 9.0})
        rec = d.record("family")
        self.assertEqual(rec["budget"], {"h_pred": 4.0})
        self.assertEqual((rec["type"], rec["zone"], rec["day"]), ("daily", "family", "2026-10-17"))

    def test_the_record_has_no_trim_fields(self):
        rec = hp.ZoneDay("2026-10-17").record("family")
        self.assertEqual([k for k in rec if "trim" in k or "bias" in k], [])

    def test_a_saved_day_from_the_old_model_loads(self):
        old = {"ticks": 3, "mode_s": {"warm": 600.0}, "call_s": 300.0, "bias": {"8": [1.0, 2]}}
        d = hp.ZoneDay("2026-10-17", old)
        self.assertEqual(d.record("family")["mode_hours"], {"warm": 0.17})

    def test_state_survives_a_json_round_trip(self):
        import json

        d = hp.ZoneDay("2026-10-17")
        for h in (8, 9, 14):
            d.add_tick(tick(hour=h, calling=True, air=22.0 + h / 100))
        d.add_would_write(T(10), 22.0)
        d.note_budget({"h_pred": 4.0})
        again = hp.ZoneDay("2026-10-17", json.loads(json.dumps(d.snapshot())))
        self.assertEqual(again.record("z"), d.record("z"))


if __name__ == "__main__":
    unittest.main()
