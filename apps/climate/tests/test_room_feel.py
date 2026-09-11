# tests/test_room_feel.py - RoomFeel: one sensor.<room>_feel per room, fused from Salus/
# FP300/ceiling/floor sources per room_feel.py's module docstring. Same __new__ +
# monkeypatched get_state/set_state/log harness as the other apps/climate/tests files.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

import room_feel as rf  # noqa: E402

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _make_app(rooms, **overrides):
    """RoomFeel instance without running initialize(). `rooms` is a dict of
    room_key -> raw (yaml-shaped) room config, parsed through RoomFeel._parse_room so
    the tests exercise the same parsing/defaulting the real yaml goes through."""
    app = rf.RoomFeel.__new__(rf.RoomFeel)
    app.rooms = {k: rf.RoomFeel._parse_room(v) for k, v in rooms.items()}
    app._room_state = {k: {"last_cmp": None} for k in app.rooms}

    app.stale_minutes = overrides.get("stale_minutes", 45.0)
    app.cold_max_c = overrides.get("cold_max_c", 18.0)
    app.cool_max_c = overrides.get("cool_max_c", 20.0)
    app.comfortable_max_c = overrides.get("comfortable_max_c", 24.0)
    app.warm_max_c = overrides.get("warm_max_c", 26.0)
    app.iaq_fresh_max = overrides.get("iaq_fresh_max", 50)
    app.iaq_good_max = overrides.get("iaq_good_max", 100)
    app.iaq_stuffy_max = overrides.get("iaq_stuffy_max", 200)
    app.eco2_fresh_max = overrides.get("eco2_fresh_max", 800)
    app.eco2_good_max = overrides.get("eco2_good_max", 1200)
    app.eco2_stuffy_max = overrides.get("eco2_stuffy_max", 2000)
    app.mould_high_gap_c = overrides.get("mould_high_gap_c", 1.0)
    app.mould_watch_gap_c = overrides.get("mould_watch_gap_c", 3.0)
    app.floor_cold_spread_c = overrides.get("floor_cold_spread_c", 2.0)
    app.outdoor_temp_entity = overrides.get("outdoor_temp_entity", "sensor.gw2000a_outdoor_temperature")
    app.outdoor_rh_entity = overrides.get("outdoor_rh_entity", "sensor.gw2000a_humidity")
    app.airing_dew_margin_c = overrides.get("airing_dew_margin_c", 1.0)
    app.airing_rh_min = overrides.get("airing_rh_min", 60.0)
    app.supply_air_source = overrides.get("supply_air_source", "sensor.ventilation_thermometer_temperature")
    app.supply_air_publish = overrides.get("supply_air_publish", "sensor.bedroom_supply_air_temperature")

    plain = dict(overrides.get("plain") or {})
    plain.setdefault("sensor.gw2000a_outdoor_temperature", "15.0")
    plain.setdefault("sensor.gw2000a_humidity", "50")
    attrs = dict(overrides.get("attrs") or {})
    last_updated = dict(overrides.get("last_updated") or {})

    def get_state(entity, attribute=None):
        if attribute == "last_updated":
            return last_updated.get(entity, NOW.isoformat())
        if attribute is not None:
            return attrs.get((entity, attribute))
        return plain.get(entity)

    app.get_state = get_state
    app.get_now = overrides.get("get_now", lambda: NOW)
    app.log = lambda *a, **kw: None

    app._captured = {}
    app._set_state_calls = []

    def set_state(entity, state=None, replace=None, attributes=None):
        app._captured[entity] = {"state": state, "attrs": attributes}
        app._set_state_calls.append(entity)

    app.set_state = set_state
    return app


class PureMedian(unittest.TestCase):
    def test_median_of_three(self):
        self.assertEqual(rf.median([20.0, 21.0, 22.0]), 21.0)

    def test_median_ignores_none(self):
        self.assertEqual(rf.median([20.0, None, 22.0]), 21.0)

    def test_median_empty_is_none(self):
        self.assertIsNone(rf.median([]))
        self.assertIsNone(rf.median([None, None]))


class DewPointAndAbsHumidity(unittest.TestCase):
    def test_dew_point_known_pair(self):
        self.assertAlmostEqual(rf.dew_point_c(22.0, 50.0), 11.1, delta=0.15)

    def test_abs_humidity_known_pair(self):
        self.assertAlmostEqual(rf.abs_humidity_g_m3(20.0, 50.0), 8.6, delta=0.1)
        self.assertAlmostEqual(rf.abs_humidity_g_m3(22.0, 50.0), 9.7, delta=0.1)

    def test_abs_humidity_invalid_input(self):
        self.assertIsNone(rf.abs_humidity_g_m3(None, 50))
        self.assertIsNone(rf.abs_humidity_g_m3(20, 0))
        self.assertIsNone(rf.abs_humidity_g_m3(20, 150))
        self.assertIsNone(rf.abs_humidity_g_m3("x", 50))


class ComfortWord(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(rf.comfort_word(17.9, 18, 20, 24, 26), "cold")
        self.assertEqual(rf.comfort_word(19.0, 18, 20, 24, 26), "cool")
        self.assertEqual(rf.comfort_word(22.0, 18, 20, 24, 26), "comfortable")
        self.assertEqual(rf.comfort_word(25.0, 18, 20, 24, 26), "warm")
        self.assertEqual(rf.comfort_word(27.0, 18, 20, 24, 26), "hot")
        self.assertEqual(rf.comfort_word(None, 18, 20, 24, 26), "unknown")


class AirBands(unittest.TestCase):
    def test_iaq_bands(self):
        self.assertEqual(rf.band_from_iaq(10, 50, 100, 200), "fresh")
        self.assertEqual(rf.band_from_iaq(80, 50, 100, 200), "good")
        self.assertEqual(rf.band_from_iaq(150, 50, 100, 200), "stuffy")
        self.assertEqual(rf.band_from_iaq(500, 50, 100, 200), "poor")
        self.assertEqual(rf.band_from_iaq(None, 50, 100, 200), "unknown")

    def test_eco2_bands(self):
        self.assertEqual(rf.band_from_eco2(500, 800, 1200, 2000), "fresh")
        self.assertEqual(rf.band_from_eco2(1000, 800, 1200, 2000), "good")
        self.assertEqual(rf.band_from_eco2(1800, 800, 1200, 2000), "stuffy")
        self.assertEqual(rf.band_from_eco2(2500, 800, 1200, 2000), "poor")
        self.assertEqual(rf.band_from_eco2(None, 800, 1200, 2000), "unknown")


class MouldRiskBand(unittest.TestCase):
    def test_high_when_floor_near_dew_point(self):
        self.assertEqual(rf.mould_risk_band(15.0, 15.5, 1.0, 3.0), "high")

    def test_watch_when_floor_close(self):
        self.assertEqual(rf.mould_risk_band(15.0, 17.5, 1.0, 3.0), "watch")

    def test_low_when_floor_well_above(self):
        self.assertEqual(rf.mould_risk_band(15.0, 20.0, 1.0, 3.0), "low")

    def test_unknown_on_missing_data(self):
        self.assertEqual(rf.mould_risk_band(None, 20.0, 1.0, 3.0), "unknown")
        self.assertEqual(rf.mould_risk_band(15.0, None, 1.0, 3.0), "unknown")


class AiringHelps(unittest.TestCase):
    def test_true_when_drier_and_humid_enough(self):
        self.assertTrue(rf.airing_helps(14.0, 65.0, 10.0, 1.0, 60.0))

    def test_false_when_outdoor_not_much_drier(self):
        self.assertFalse(rf.airing_helps(14.0, 65.0, 13.5, 1.0, 60.0))

    def test_false_when_room_not_humid_enough(self):
        self.assertFalse(rf.airing_helps(14.0, 40.0, 10.0, 1.0, 60.0))

    def test_false_on_missing_data(self):
        self.assertFalse(rf.airing_helps(None, 65.0, 10.0, 1.0, 60.0))


class TimeWindow(unittest.TestCase):
    def test_inside_window(self):
        from datetime import time as dtime
        self.assertTrue(rf.in_time_window(dtime(8, 0), "07:30", "10:00"))

    def test_outside_window(self):
        from datetime import time as dtime
        self.assertFalse(rf.in_time_window(dtime(11, 0), "07:30", "10:00"))
        self.assertFalse(rf.in_time_window(dtime(7, 0), "07:30", "10:00"))

    def test_garbage_is_false(self):
        from datetime import time as dtime
        self.assertFalse(rf.in_time_window(dtime(8, 0), "garbage", "10:00"))


BEDROOM_CFG = {
    "air_sources": [
        {"entity": "climate.bedroom_thermostat", "attribute": "current_temperature",
         "rh_entity": "sensor.bedroom_humidity", "label": "salus"},
        {"entity": "sensor.bedroom_presence_temperature",
         "rh_entity": "sensor.bedroom_presence_humidity", "label": "fp300"},
    ],
    "floor_entity": "sensor.bedroom_floor_thermometer_temperature",
    "floor_rh_entity": "sensor.bedroom_floor_thermometer_humidity",
    "window_contacts": ["binary_sensor.bedroom_window_contact"],
}

KITCHEN_CFG = {
    "air_sources": [
        {"entity": "sensor.kitchen_presence_temperature",
         "rh_entity": "sensor.kitchen_presence_humidity", "label": "fp300", "sun_flag": True},
        {"entity": "climate.family_room_thermostat", "attribute": "current_temperature",
         "rh_entity": "climate.family_room_thermostat", "rh_attribute": "current_humidity",
         "label": "salus_family"},
        {"entity": "sensor.kitchen_smoke_alarm_temperature",
         "rh_entity": "sensor.kitchen_smoke_alarm_humidity", "label": "ceiling",
         "offset_c": -1.5, "offset_rh": 6.5},
    ],
    "window_contacts": ["binary_sensor.kitchen_window_contact"],
    "air_quality": {"aqi_entity": "sensor.kitchen_smoke_alarm_aqi",
                     "eco2_entity": "sensor.kitchen_smoke_alarm_eco2"},
    "sun_hit": {"radiation_entity": "sensor.gw2000a_solar_radiation",
                "threshold_w_m2": 150, "start": "07:30", "end": "10:00"},
}

DINING_CFG = {
    "air_sources": [
        {"entity": "sensor.dining_room_presence_temperature",
         "rh_entity": "sensor.dining_room_presence_humidity", "label": "fp300", "sun_flag": True},
        {"entity": "climate.family_room_thermostat", "attribute": "current_temperature",
         "rh_entity": "climate.family_room_thermostat", "rh_attribute": "current_humidity",
         "label": "salus_family"},
    ],
    "floor_entity": "sensor.dining_room_floor_thermometer_temperature",
    "floor_rh_entity": "sensor.dining_room_floor_thermometer_humidity",
    "window_contacts": [
        "binary_sensor.dining_room_window_1_contact",
        "binary_sensor.dining_room_window_2_contact",
        "binary_sensor.dining_room_window_3_contact",
    ],
    "sun_hit": {"radiation_entity": "sensor.gw2000a_solar_radiation",
                "threshold_w_m2": 150, "start": "06:30", "end": "09:00"},
}

BATHROOM_CFG = {
    "air_sources": [
        {"entity": "climate.bathroom_thermostat", "attribute": "current_temperature",
         "rh_entity": "sensor.bathroom_humidity", "label": "salus"},
    ],
    "floor_entity": "sensor.bathroom_floor_thermometer_temperature",
    "floor_rh_entity": "sensor.bathroom_floor_thermometer_humidity",
    "window_contacts": ["binary_sensor.bathroom_window_contact"],
    "mould_risk": True,
}


class MedianOfThreeSources(unittest.TestCase):
    """Kitchen: FP300 + family Salus + ceiling, all readable, no sun_hit -> median of 3."""

    def test_temp_and_rh_are_medians(self):
        app = _make_app({"kitchen": KITCHEN_CFG}, plain={
            "sensor.kitchen_presence_temperature": "21.0",
            "sensor.kitchen_presence_humidity": "50",
            "climate.family_room_thermostat": "off",
            "sensor.kitchen_smoke_alarm_temperature": "23.0",
            "sensor.kitchen_smoke_alarm_humidity": "40",
        }, attrs={
            ("climate.family_room_thermostat", "current_temperature"): 22.0,
            ("climate.family_room_thermostat", "current_humidity"): 45,
        }, get_now=lambda: NOW.replace(hour=12))
        app._evaluate_room("kitchen")
        a = app._captured["sensor.kitchen_feel"]["attrs"]
        # temps: 21.0, 22.0, (23.0 - 1.5 = 21.5) -> median 21.5
        self.assertEqual(a["temp_c"], 21.5)
        self.assertEqual(app._captured["sensor.kitchen_feel"]["state"], 21.5)
        # rh: 50, 45, (40 + 6.5 = 46.5) -> median 46.5 -> rounded to 0dp -> 46 or 47
        self.assertEqual(a["rh"], round(46.5, 0))
        # 3 temp entities + 2 distinct rh entities (family Salus shares one entity for
        # both temp and rh, so it contributes a single id, not two).
        self.assertEqual(len(a["sources"]), 5)
        self.assertEqual(
            set(a["sources"]),
            {
                "sensor.kitchen_presence_temperature",
                "sensor.kitchen_presence_humidity",
                "climate.family_room_thermostat",
                "sensor.kitchen_smoke_alarm_temperature",
                "sensor.kitchen_smoke_alarm_humidity",
            },
        )
        self.assertEqual(a["excluded"], ["<none>"])
        self.assertIn("median of 3", a["reason"])


class CeilingOffsetApplied(unittest.TestCase):
    """The ceiling (Twinguard) source is offset -1.5C / +6.5RH BEFORE it enters the
    median - proven by pinning the other two sources to the ceiling's own adjusted value."""

    def test_ceiling_reading_is_corrected_before_median(self):
        app = _make_app({"kitchen": KITCHEN_CFG}, plain={
            "sensor.kitchen_presence_temperature": "20.0",
            "sensor.kitchen_presence_humidity": "38.5",
            "climate.family_room_thermostat": "off",
            "sensor.kitchen_smoke_alarm_temperature": "21.5",  # -1.5 -> 20.0
            "sensor.kitchen_smoke_alarm_humidity": "32.0",     # +6.5 -> 38.5
        }, attrs={
            ("climate.family_room_thermostat", "current_temperature"): 20.0,
            ("climate.family_room_thermostat", "current_humidity"): 38.5,
        }, get_now=lambda: NOW.replace(hour=12))
        app._evaluate_room("kitchen")
        a = app._captured["sensor.kitchen_feel"]["attrs"]
        self.assertEqual(a["temp_c"], 20.0)
        self.assertEqual(a["rh"], 38.0)  # round(38.5, 0) -> banker's rounding to 38


class SunHitExclusion(unittest.TestCase):
    def test_sun_hit_excludes_flagged_fp300_when_others_remain(self):
        app = _make_app({"kitchen": KITCHEN_CFG}, plain={
            "sensor.kitchen_presence_temperature": "26.0",
            "sensor.kitchen_presence_humidity": "30",
            "climate.family_room_thermostat": "off",
            "sensor.kitchen_smoke_alarm_temperature": "23.0",
            "sensor.kitchen_smoke_alarm_humidity": "40",
            "sensor.gw2000a_solar_radiation": "300",
        }, attrs={
            ("climate.family_room_thermostat", "current_temperature"): 22.0,
            ("climate.family_room_thermostat", "current_humidity"): 45,
        }, get_now=lambda: NOW.replace(hour=8, minute=30))
        app._evaluate_room("kitchen")
        a = app._captured["sensor.kitchen_feel"]["attrs"]
        self.assertEqual(a["sun_hit"], "true")
        self.assertIn("sensor.kitchen_presence_temperature: sun_hit", a["excluded"])
        self.assertNotIn("sensor.kitchen_presence_temperature", a["sources"])
        # remaining: family salus 22.0, ceiling 23.0-1.5=21.5 -> median 21.75 -> 21.8
        self.assertEqual(a["temp_c"], 21.8)

    def test_sun_hit_below_threshold_does_not_exclude(self):
        app = _make_app({"kitchen": KITCHEN_CFG}, plain={
            "sensor.kitchen_presence_temperature": "26.0",
            "sensor.kitchen_presence_humidity": "30",
            "climate.family_room_thermostat": "off",
            "sensor.kitchen_smoke_alarm_temperature": "23.0",
            "sensor.kitchen_smoke_alarm_humidity": "40",
            "sensor.gw2000a_solar_radiation": "50",
        }, attrs={
            ("climate.family_room_thermostat", "current_temperature"): 22.0,
            ("climate.family_room_thermostat", "current_humidity"): 45,
        }, get_now=lambda: NOW.replace(hour=8, minute=30))
        app._evaluate_room("kitchen")
        a = app._captured["sensor.kitchen_feel"]["attrs"]
        self.assertEqual(a["sun_hit"], "false")
        self.assertEqual(a["excluded"], ["<none>"])

    def test_sun_hit_never_excludes_the_last_remaining_source(self):
        cfg = dict(KITCHEN_CFG)
        app = _make_app({"kitchen": cfg}, plain={
            "sensor.kitchen_presence_temperature": "26.0",
            "sensor.kitchen_presence_humidity": "30",
            "climate.family_room_thermostat": "unavailable",
            "sensor.kitchen_smoke_alarm_temperature": "unavailable",
            "sensor.kitchen_smoke_alarm_humidity": "unavailable",
            "sensor.gw2000a_solar_radiation": "300",
        }, get_now=lambda: NOW.replace(hour=8, minute=30))
        app._evaluate_room("kitchen")
        a = app._captured["sensor.kitchen_feel"]["attrs"]
        self.assertEqual(a["temp_c"], 26.0)
        self.assertIn("sensor.kitchen_presence_temperature", a["sources"])


class FloorOnlyFallback(unittest.TestCase):
    def test_all_air_sources_dead_uses_floor_plus_half(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "unavailable",
            "sensor.bedroom_humidity": "unavailable",
            "sensor.bedroom_presence_temperature": "unknown",
            "sensor.bedroom_presence_humidity": "unknown",
            "sensor.bedroom_floor_thermometer_temperature": "19.0",
            "sensor.bedroom_floor_thermometer_humidity": "55",
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["temp_c"], 19.5)
        self.assertEqual(a["sources"], ["sensor.bedroom_floor_thermometer_temperature",
                                         "sensor.bedroom_floor_thermometer_humidity"])
        self.assertIn("floor+0.5", a["reason"])
        self.assertEqual(len(a["excluded"]), 2)


class TotalBlackout(unittest.TestCase):
    def test_no_air_or_floor_publishes_unavailable_never_zero(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "unavailable",
            "sensor.bedroom_humidity": "unavailable",
            "sensor.bedroom_presence_temperature": "unavailable",
            "sensor.bedroom_presence_humidity": "unavailable",
            "sensor.bedroom_floor_thermometer_temperature": "unavailable",
        })
        app._evaluate_room("bedroom")
        cap = app._captured["sensor.bedroom_feel"]
        self.assertEqual(cap["state"], "unavailable")
        self.assertIsNone(cap["attrs"]["temp_c"])
        self.assertNotEqual(cap["state"], 0)
        self.assertNotEqual(cap["state"], 0.0)


class UnavailableSourceExcluded(unittest.TestCase):
    def test_one_bad_source_excluded_others_still_used_never_zero(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "unavailable",
            "sensor.bedroom_humidity": "unavailable",
            "sensor.bedroom_presence_temperature": "21.0",
            "sensor.bedroom_presence_humidity": "50",
            "sensor.bedroom_floor_thermometer_temperature": "19.5",
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["temp_c"], 21.0)
        self.assertNotEqual(a["temp_c"], 0)
        self.assertIn("climate.bedroom_thermostat: unavailable", a["excluded"])
        self.assertEqual(a["reason"], "single air source: sensor.bedroom_presence_temperature")


class WindowOpenOr(unittest.TestCase):
    def test_true_when_any_contact_open(self):
        app = _make_app({"dining_room": DINING_CFG}, plain={
            "sensor.dining_room_presence_temperature": "20.0",
            "sensor.dining_room_presence_humidity": "45",
            "climate.family_room_thermostat": "off",
            "binary_sensor.dining_room_window_1_contact": "off",
            "binary_sensor.dining_room_window_2_contact": "on",
            "binary_sensor.dining_room_window_3_contact": "off",
        }, attrs={
            ("climate.family_room_thermostat", "current_temperature"): 20.0,
            ("climate.family_room_thermostat", "current_humidity"): 45,
        })
        app._evaluate_room("dining_room")
        a = app._captured["sensor.dining_room_feel"]["attrs"]
        self.assertEqual(a["window_open"], "true")

    def test_false_when_all_closed(self):
        app = _make_app({"dining_room": DINING_CFG}, plain={
            "sensor.dining_room_presence_temperature": "20.0",
            "sensor.dining_room_presence_humidity": "45",
            "climate.family_room_thermostat": "off",
            "binary_sensor.dining_room_window_1_contact": "off",
            "binary_sensor.dining_room_window_2_contact": "off",
            "binary_sensor.dining_room_window_3_contact": "off",
        }, attrs={
            ("climate.family_room_thermostat", "current_temperature"): 20.0,
            ("climate.family_room_thermostat", "current_humidity"): 45,
        })
        app._evaluate_room("dining_room")
        a = app._captured["sensor.dining_room_feel"]["attrs"]
        self.assertEqual(a["window_open"], "false")


class AiringHelpsIntegration(unittest.TestCase):
    def test_room_marks_airing_helps_when_outdoor_drier(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "unavailable",
            "sensor.bedroom_humidity": "unavailable",
            "sensor.bedroom_presence_temperature": "23.0",
            "sensor.bedroom_presence_humidity": "70",
            "sensor.bedroom_floor_thermometer_temperature": "21.0",
            "sensor.gw2000a_outdoor_temperature": "15.0",
            "sensor.gw2000a_humidity": "40",
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["airing_helps"], "true")

    def test_room_marks_airing_helps_false_when_room_is_dry(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "unavailable",
            "sensor.bedroom_humidity": "unavailable",
            "sensor.bedroom_presence_temperature": "23.0",
            "sensor.bedroom_presence_humidity": "35",
            "sensor.bedroom_floor_thermometer_temperature": "21.0",
            "sensor.gw2000a_outdoor_temperature": "15.0",
            "sensor.gw2000a_humidity": "40",
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["airing_helps"], "false")


class MouldRiskIntegration(unittest.TestCase):
    def test_bathroom_high_risk(self):
        app = _make_app({"bathroom": BATHROOM_CFG}, plain={
            "climate.bathroom_thermostat": "22.0",
            "sensor.bathroom_humidity": "85",
            "sensor.bathroom_floor_thermometer_temperature": "18.5",
        }, attrs={
            ("climate.bathroom_thermostat", "current_temperature"): 22.0,
        })
        app._evaluate_room("bathroom")
        a = app._captured["sensor.bathroom_feel"]["attrs"]
        self.assertEqual(a["mould_risk"], "high")

    def test_non_bathroom_room_reports_not_applicable(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "22.0",
            "sensor.bedroom_humidity": "50",
            "sensor.bedroom_presence_temperature": "22.0",
            "sensor.bedroom_presence_humidity": "50",
        }, attrs={
            ("climate.bedroom_thermostat", "current_temperature"): 22.0,
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["mould_risk"], "n/a")


class StaleFlag(unittest.TestCase):
    def test_stale_true_when_newest_used_source_is_old(self):
        old_ts = (NOW - timedelta(minutes=90)).isoformat()
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "22.0",
            "sensor.bedroom_humidity": "50",
            "sensor.bedroom_presence_temperature": "unavailable",
            "sensor.bedroom_presence_humidity": "unavailable",
        }, attrs={
            ("climate.bedroom_thermostat", "current_temperature"): 22.0,
        }, last_updated={
            "climate.bedroom_thermostat": old_ts,
            "sensor.bedroom_humidity": old_ts,
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["stale"], "true")

    def test_stale_false_when_fresh(self):
        fresh_ts = (NOW - timedelta(minutes=5)).isoformat()
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "22.0",
            "sensor.bedroom_humidity": "50",
            "sensor.bedroom_presence_temperature": "unavailable",
            "sensor.bedroom_presence_humidity": "unavailable",
        }, attrs={
            ("climate.bedroom_thermostat", "current_temperature"): 22.0,
        }, last_updated={
            "climate.bedroom_thermostat": fresh_ts,
            "sensor.bedroom_humidity": fresh_ts,
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["stale"], "false")

    def test_stale_defaults_false_without_usable_timestamps(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain={
            "climate.bedroom_thermostat": "22.0",
            "sensor.bedroom_humidity": "50",
            "sensor.bedroom_presence_temperature": "unavailable",
            "sensor.bedroom_presence_humidity": "unavailable",
        }, attrs={
            ("climate.bedroom_thermostat", "current_temperature"): 22.0,
        })
        app._evaluate_room("bedroom")
        a = app._captured["sensor.bedroom_feel"]["attrs"]
        self.assertEqual(a["stale"], "false")


class PublishOnlyOnChange(unittest.TestCase):
    def _plain_bedroom(self):
        return {
            "climate.bedroom_thermostat": "22.0",
            "sensor.bedroom_humidity": "50",
            "sensor.bedroom_presence_temperature": "22.0",
            "sensor.bedroom_presence_humidity": "50",
            "binary_sensor.bedroom_window_contact": "off",
        }

    def test_second_identical_evaluate_does_not_republish(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain=self._plain_bedroom(), attrs={
            ("climate.bedroom_thermostat", "current_temperature"): 22.0,
        })
        app._evaluate_room("bedroom")
        app._evaluate_room("bedroom")
        self.assertEqual(app._set_state_calls.count("sensor.bedroom_feel"), 1)

    def test_force_republishes_even_without_a_change(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain=self._plain_bedroom(), attrs={
            ("climate.bedroom_thermostat", "current_temperature"): 22.0,
        })
        app._evaluate_room("bedroom")
        app._evaluate_room("bedroom", force=True)
        self.assertEqual(app._set_state_calls.count("sensor.bedroom_feel"), 2)

    def test_changed_attribute_triggers_republish(self):
        app = _make_app({"bedroom": BEDROOM_CFG}, plain=self._plain_bedroom(), attrs={
            ("climate.bedroom_thermostat", "current_temperature"): 22.0,
        })
        app._evaluate_room("bedroom")
        plain2 = self._plain_bedroom()
        plain2["binary_sensor.bedroom_window_contact"] = "on"

        def get_state(entity, attribute=None):
            if attribute == "last_updated":
                return NOW.isoformat()
            if attribute is not None:
                return {("climate.bedroom_thermostat", "current_temperature"): 22.0}.get((entity, attribute))
            return plain2.get(entity)

        app.get_state = get_state
        app._evaluate_room("bedroom")
        self.assertEqual(app._set_state_calls.count("sensor.bedroom_feel"), 2)
        self.assertEqual(app._captured["sensor.bedroom_feel"]["attrs"]["window_open"], "true")


class SupplyAirMirror(unittest.TestCase):
    def test_mirrors_source_value(self):
        app = _make_app({}, plain={"sensor.ventilation_thermometer_temperature": "21.3"})
        app._evaluate_supply_air()
        cap = app._captured["sensor.bedroom_supply_air_temperature"]
        self.assertEqual(cap["state"], 21.3)
        self.assertEqual(cap["attrs"]["source_entities"], ["sensor.ventilation_thermometer_temperature"])

    def test_unavailable_source_mirrors_as_unavailable_not_zero(self):
        app = _make_app({}, plain={"sensor.ventilation_thermometer_temperature": "unavailable"})
        app._evaluate_supply_air()
        cap = app._captured["sensor.bedroom_supply_air_temperature"]
        self.assertEqual(cap["state"], "unavailable")
        self.assertNotEqual(cap["state"], 0)


class TechnicalRoomFeel(unittest.TestCase):
    def test_single_ecowitt_source(self):
        cfg = {
            "technical_room": {
                "air_sources": [
                    {"entity": "sensor.ecowitt_temperature_indoor",
                     "rh_entity": "sensor.ecowitt_humidity_humidityin", "label": "ecowitt"},
                ],
                "window_contacts": [],
            }
        }
        app = _make_app(cfg, plain={
            "sensor.ecowitt_temperature_indoor": "24.0",
            "sensor.ecowitt_humidity_humidityin": "45",
        })
        app._evaluate_room("technical_room")
        cap = app._captured["sensor.technical_room_feel"]
        self.assertEqual(cap["state"], 24.0)
        self.assertEqual(cap["attrs"]["rh"], 45.0)
        self.assertEqual(cap["attrs"]["reason"], "single air source: sensor.ecowitt_temperature_indoor")


class ParseRoomDefaults(unittest.TestCase):
    def test_missing_optional_keys_default_safely(self):
        cfg = rf.RoomFeel._parse_room({"air_sources": [{"entity": "sensor.x"}]})
        self.assertIsNone(cfg["floor_entity"])
        self.assertIsNone(cfg["sun_hit"])
        self.assertIsNone(cfg["air_quality"])
        self.assertFalse(cfg["mould_risk"])
        self.assertEqual(cfg["window_contacts"], [])
        self.assertEqual(cfg["air_sources"][0]["sun_flag"], False)
        self.assertEqual(cfg["air_sources"][0]["offset_c"], 0.0)


if __name__ == "__main__":
    unittest.main()
