# tests/test_heating_plan.py - the floor-heating controller's planning logic (heating_plan.py):
# config parsing, comfort target and lead shift, mode precedence, budget and sun cut, party,
# bedroom loop, window tracker, trim, write bookkeeping and daily statistics. Pure logic, so no
# AppDaemon harness is needed.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

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
    "control_air": ["sensor.living_room_feel", "sensor.dining_room_feel"],
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
    "trim": {"initial": 0.5, "min": -0.5, "max": 1.5},
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
    "trim": {"initial": 0.0, "min": -1.0, "max": 1.0},
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
        trim=zc.trim.initial,
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
        self.assertEqual(CFG.writes_per_hour, 2)
        self.assertEqual(CFG.keypad_relock_minutes, 15)
        self.assertEqual(CFG.window.open_debounce_minutes, 2)
        self.assertEqual(CFG.bedroom.ceiling_default_c, 21.0)
        self.assertEqual(CFG.bedroom.release_below_c, 20.5)
        self.assertEqual(CFG.party.lead_hours, 8)

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

    def test_protected_minimum(self):
        fam, kri, bath = (CFG.zones[z] for z in ("family", "kristines_room", "bathroom"))
        self.assertEqual(hp.protected_min(fam, 21.5, 0.0, 19.0), 20.5)
        self.assertEqual(hp.protected_min(kri, 21.5, 0.5, 19.0), 21.5)
        self.assertEqual(hp.protected_min(bath, 22.0, 0.0, 19.0), 19.0)
        # never below the lower limit
        self.assertEqual(hp.protected_min(kri, 19.2, 0.0, 19.0), 19.0)


class QuantizeAndClamp(unittest.TestCase):
    def test_rounds_to_the_device_step_half_up(self):
        self.assertEqual(hp.quantize(21.24, None), 21.0)
        self.assertEqual(hp.quantize(21.25, None), 21.5)
        self.assertEqual(hp.quantize(21.74, None), 21.5)
        self.assertEqual(hp.quantize(21.75, None), 22.0)
        self.assertEqual(hp.quantize(22.0, None), 22.0)

    def test_keeps_the_last_value_while_within_the_hysteresis(self):
        self.assertEqual(hp.quantize(21.8, 21.5), 21.5)
        self.assertEqual(hp.quantize(21.15, 21.5), 21.5)
        self.assertEqual(hp.quantize(21.9, 21.5), 22.0)
        self.assertEqual(hp.quantize(21.05, 21.5), 21.0)

    def test_a_value_hovering_between_two_steps_does_not_flap(self):
        last = None
        seen = set()
        for raw in (21.7, 21.8, 21.76, 21.6, 21.72, 21.8, 21.7):
            last = hp.quantize(raw, last)
            seen.add(last)
        self.assertEqual(len(seen), 1)

    def test_clamp(self):
        self.assertEqual(hp.clamp(25.5, 19.0, 24.0), 24.0)
        self.assertEqual(hp.clamp(10.0, 19.0, 24.0), 19.0)
        self.assertEqual(hp.clamp(21.0, 19.0, 24.0), 21.0)


class SetpointByTime(unittest.TestCase):
    """The family day: trim 0.5, lead 4 h, so the setpoint leads the profile by four hours."""

    def sp(self, hour, minute=0, **kw):
        return plan_at(T(hour, minute), **kw)

    def test_hold_follows_the_shifted_profile(self):
        expected = {(0, 0): 21.5, (5, 59): 21.5, (6, 0): 22.0, (12, 59): 22.0, (13, 0): 22.5}
        expected.update({(18, 59): 22.5, (19, 0): 21.5, (23, 59): 21.5})
        for (h, m), want in expected.items():
            p = self.sp(h, m)
            self.assertEqual((p.mode, p.setpoint), ("hold", want), (h, m))

    def test_the_decided_target_numbers(self):
        self.assertEqual(self.sp(12).target, 21.5)
        self.assertEqual(self.sp(18).target, 22.0)
        self.assertEqual(self.sp(2).target, 21.0)

    def test_resident_room_is_flat_and_moves_with_the_nudge(self):
        for h in (3, 12, 20):
            self.assertEqual(plan_at(T(h), "kristines_room", nudge=0.0).setpoint, 21.5)
            self.assertEqual(plan_at(T(h), "kristines_room", nudge=-1.0).setpoint, 20.5)
        self.assertEqual(plan_at(T(12), "claudias_room", nudge=1.0).setpoint, 22.5)

    def test_guest_bathroom_is_flat_at_its_own_level(self):
        for h in (3, 12, 20):
            self.assertEqual(plan_at(T(h), "guest_bathroom").setpoint, 20.0)

    def test_bathroom_has_no_overnight_block(self):
        for minutes in range(0, 24 * 60, 5):
            p = plan_at(T(minutes // 60, minutes % 60), "bathroom")
            if 6 * 60 <= minutes < 9 * 60:
                self.assertEqual(p.setpoint, 22.0, minutes)
            else:
                self.assertEqual(p.setpoint, 19.0, minutes)

    def test_setpoint_is_clamped_to_the_limits(self):
        p = plan_at(T(12), base=24.0, trim=1.5)
        self.assertEqual(p.raw, 25.5)
        self.assertEqual(p.setpoint, 24.0)
        p = plan_at(T(12), base=18.0, trim=-0.5)
        self.assertEqual(p.setpoint, 19.0)

    def test_effective_limits_can_be_tighter_than_the_configured_ones(self):
        p = plan_at(T(12), lower_c=21.0, upper_c=21.5, trim=0.5)
        self.assertEqual(p.setpoint, 21.5)
        self.assertEqual(plan_at(T(8, 0), "bathroom", lower_c=19.5).setpoint, 22.0)
        self.assertEqual(plan_at(T(2, 0), "bathroom", lower_c=19.5).setpoint, 19.5)

    def test_last_planned_value_holds_a_hovering_setpoint(self):
        p = plan_at(T(12), trim=0.8, last_planned=22.0)  # raw 22.3 is within 0.4 of 22.0
        self.assertEqual(p.setpoint, 22.0)
        p = plan_at(T(12), trim=0.8)
        self.assertEqual(p.setpoint, 22.5)


class ModePrecedence(unittest.TestCase):
    """window > settle > party > guard > sun_cut > fallback > hold, behind the season gate."""

    zone = replace(CFG.zones["family"], guard=hp.GuardConfig(True, "lower_limit"))

    def mode_of(self, **kw):
        base = dict(
            fallback=True,
            hold_back=True,
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
        self.assertEqual(self.mode_of(party_windows=()), "guard")
        self.assertEqual(self.mode_of(party_windows=(), hold_back=False), "fallback")
        self.assertEqual(self.mode_of(party_windows=(), hold_back=False, fallback=False), "sun_cut")
        self.assertEqual(
            self.mode_of(party_windows=(), hold_back=False, fallback=False, cut_windows=()), "hold"
        )

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
        self.assertEqual((p.mode, p.plan_mode, p.setpoint), ("off_season", "hold", 22.0))
        p = plan_at(T(12), online=False)
        self.assertEqual((p.mode, p.plan_mode), ("device_offline", "hold"))
        p = plan_at(T(12), season_on=False, online=False)
        self.assertEqual(p.mode, "off_season")
        self.assertIn("Heating season is off", p.reasons[0])

    def test_window_party_and_settle_sit_at_the_lower_limit(self):
        for kw in (
            dict(window_state="open"),
            dict(window_state="settle"),
            dict(party_windows=((T(8), T(18)),)),
        ):
            p = plan_at(T(12), **kw)
            self.assertEqual((p.setpoint, p.aim), (19.0, None), kw)

    def test_sun_cut_holds_the_protected_minimum_plus_trim(self):
        cut = ((T(1), T(14), "2026-10-17"),)
        p = plan_at(T(4), cut_windows=cut)
        self.assertEqual((p.mode, p.aim, p.setpoint), ("sun_cut", 20.5, 21.0))
        # the cut never raises the plan: an aim below the protected minimum stays where it is
        p = plan_at(T(4), cut_windows=cut, base=19.5)
        self.assertEqual((p.target, p.aim), (19.0, 19.0))

    def test_cut_is_ignored_for_a_zone_without_sun_cut(self):
        cut = ((T(1), T(14), "2026-10-17"),)
        self.assertEqual(plan_at(T(4), "kristines_room", cut_windows=cut).mode, "hold")

    def test_fallback_is_a_constant_without_profile(self):
        p = plan_at(T(13), fallback=True)  # hold would be 22.5 here
        self.assertEqual((p.mode, p.aim, p.setpoint), ("fallback", 21.5, 22.0))
        p = plan_at(T(20), fallback=True, nudge=0.5)
        self.assertEqual(p.setpoint, 22.5)

    def test_bathroom_fallback_stays_at_the_lower_limit(self):
        p = plan_at(T(3), "bathroom", fallback=True)
        self.assertEqual((p.mode, p.setpoint), ("fallback", 19.0))

    def test_bedroom_hold_back_keeps_the_bathroom_at_the_lower_limit(self):
        p = plan_at(T(7), "bathroom", hold_back=True)
        self.assertEqual((p.mode, p.setpoint), ("guard", 19.0))
        p = plan_at(T(7), "bathroom", hold_back=False)
        self.assertEqual((p.mode, p.setpoint), ("hold", 22.0))

    def test_hold_back_does_not_touch_the_family_zone(self):
        p = plan_at(T(4), "family", hold_back=True)
        self.assertEqual(p.mode, "hold")
        self.assertEqual(p.setpoint, 21.5)

    def test_window_in_the_bathroom_beats_the_hold_back_and_the_morning(self):
        self.assertEqual(plan_at(T(7), "bathroom", window_state="open").mode, "window")


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
        self.assertAlmostEqual(b.skipped_h, 0.334 * 13, delta=0.05)
        self.assertAlmostEqual(b.ratio, 1.31, delta=0.02)
        cut = hp.evaluate_cut(self.zc, [b], T(2), None)
        self.assertEqual(cut.windows, ((T(1), T(14), "2026-10-17"),))
        self.assertEqual(cut.active_day, "2026-10-17")

    def test_example_sunny_december_day_does_not_cut(self):
        b = self.budget(dw(-1.0, 15.0, 1.2), range(11, 14))
        self.assertAlmostEqual(b.h_dull, 13.1, delta=0.1)
        self.assertAlmostEqual(b.u_eq, 0.55, delta=0.01)
        self.assertAlmostEqual(b.s_sun, 1.9, delta=0.1)
        self.assertAlmostEqual(b.ratio, 0.36, delta=0.02)
        self.assertEqual(hp.evaluate_cut(self.zc, [b], T(5), None).windows, ())

    def test_claudia_in_december_cuts_at_full_trust_and_not_at_half(self):
        zc = CFG.zones["claudias_room"]
        weather = dw(-1.0, 15.0, 1.2)
        b = self.budget(weather, [12, 13], zc=zc)
        self.assertAlmostEqual(b.h_dull, 11.3, delta=0.1)
        self.assertAlmostEqual(b.s_sun, 3.3, delta=0.1)
        self.assertEqual((b.window_start, b.window_end), (T(4), T(13)))
        self.assertAlmostEqual(b.ratio, 0.78, delta=0.02)
        self.assertEqual(len(hp.evaluate_cut(zc, [b], T(5), None).windows), 1)
        half = self.budget(weather, [12, 13], factor=0.5, zc=zc)
        self.assertAlmostEqual(half.ratio, 0.39, delta=0.02)
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

    def test_a_day_with_hardly_any_heat_to_skip_is_not_cut(self):
        b = self.budget(dw(16.5, 5.0, 3.5), range(9, 15))
        self.assertLess(b.skipped_h, 0.5)
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
        self.assertEqual((p.mode, p.setpoint), ("party", 19.0))
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

    def test_a_long_opening_is_restored_at_once_without_settling(self):
        tr = self.tracker()
        t0 = T(12)
        events = self.drive(
            tr,
            t0,
            [
                (0, {self.A: "on"}, 21.5),
                (120, {self.A: "on"}, 21.5),
                (31 * 60, {self.A: "off"}, 18.0),
            ],
        )
        self.assertEqual(events[-1], (31 * 60, hp.EVENT_RESTORED))
        self.assertEqual(tr.state, "closed")

    def test_thirty_minutes_exactly_is_still_an_airing(self):
        tr = self.tracker()
        t0 = T(12)
        events = self.drive(
            tr,
            t0,
            [(0, {self.A: "on"}, 21.5), (120, {self.A: "on"}, 21.5), (1800, {self.A: "off"}, 20.0)],
        )
        self.assertEqual(events[-1], (1800, hp.EVENT_SETTLE))

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
        self.assertEqual(tr.opened_at, t0 + 700)

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

    def test_garbage_saved_state_means_closed(self):
        tr = hp.WindowTracker(hp.WindowConfig(), {"state": "bogus", "opened_at": 5})
        self.assertEqual(tr.state, "closed")


class TokenBucketTests(unittest.TestCase):
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


class FamilyDayReplay(unittest.TestCase):
    """A whole constant-weather day through plan, gate and bucket, as the shadow run does."""

    def replay(self, zone, **kw):
        last, desired, since = None, None, T(0)
        bucket = hp.TokenBucket(2)
        writes = []
        for step in range(0, 24 * 12):
            now = T(0) + step * 300
            p = hp.plan(CFG.zones[zone], inputs(zone, last_planned=last, **kw), now, TZ)
            if p.setpoint != desired:
                desired, since = p.setpoint, now
            if last is None:
                last = desired  # the state file already holds yesterday's evening value
            g = hp.write_gate(desired, last, now, since, None, bucket, 600)
            if g.write:
                last = desired
                writes.append((hp.hhmm(now, TZ), desired))
        return writes

    def test_family_makes_three_changes_ten_minutes_after_each_step(self):
        self.assertEqual(self.replay("family"), [("06:10", 22.0), ("13:10", 22.5), ("19:10", 21.5)])

    def test_project_blocks_gives_the_same_day(self):
        zc = CFG.zones["family"]
        blocks = hp.project_blocks(zc, inputs(), T(0), T(0, day=18), TZ)
        self.assertEqual(
            [(hp.hhmm(b[0], TZ), hp.hhmm(b[1], TZ), b[2], b[3]) for b in blocks],
            [
                ("00:00", "06:00", "hold", 21.5),
                ("06:00", "13:00", "hold", 22.0),
                ("13:00", "19:00", "hold", 22.5),
                ("19:00", "00:00", "hold", 21.5),
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
                ("00:00", "hold", 21.5),
                ("01:00", "sun_cut", 21.0),
                ("14:00", "hold", 22.5),
                ("19:00", "hold", 21.5),
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


class TrimTests(unittest.TestCase):
    cfg = hp.TrimConfig(initial=0.5, minimum=-0.5, maximum=1.5)

    @staticmethod
    def hours(bias, n_hours=10, ticks=12, first=7):
        return {h: [bias * ticks, ticks] for h in range(first, first + n_hours)}

    def test_moves_halfway_to_the_measured_bias(self):
        u = hp.update_trim(0.5, self.hours(0.9), self.cfg)
        self.assertEqual((u.trim, u.valid_hours, u.changed), (0.7, 10, True))
        self.assertAlmostEqual(u.bias, 0.9)

    def test_rounded_to_five_hundredths(self):
        u = hp.update_trim(0.5, self.hours(0.77), self.cfg)
        self.assertEqual(u.trim, 0.65)  # 0.635 -> nearest 0.05

    def test_too_few_valid_hours_leave_it_alone(self):
        u = hp.update_trim(0.5, self.hours(1.2, n_hours=5), self.cfg)
        self.assertEqual((u.trim, u.bias, u.valid_hours, u.changed), (0.5, None, 5, False))

    def test_an_hour_needs_six_samples(self):
        hours = self.hours(1.2, n_hours=6)
        hours[7] = [1.2 * 5, 5]
        self.assertFalse(hp.update_trim(0.5, hours, self.cfg).changed)

    def test_only_the_trim_hours_count(self):
        hours = {h: [1.0 * 12, 12] for h in (3, 4, 5, 6, 23, 0)}
        self.assertEqual(hp.update_trim(0.5, hours, self.cfg).valid_hours, 0)
        hours = {str(h): [1.0 * 12, 12] for h in range(7, 13)}  # json keys are strings
        self.assertEqual(hp.update_trim(0.5, hours, self.cfg).valid_hours, 6)

    def test_clamped_to_the_zone_range(self):
        self.assertEqual(hp.update_trim(1.5, self.hours(4.0), self.cfg).trim, 1.5)
        self.assertEqual(hp.update_trim(-0.5, self.hours(-4.0), self.cfg).trim, -0.5)

    def test_a_zone_whose_air_is_its_thermostat_stays_at_zero(self):
        cfg = hp.TrimConfig(0.0, -1.0, 1.0)
        u = hp.update_trim(0.0, self.hours(0.0), cfg)
        self.assertEqual((u.trim, u.changed), (0.0, False))

    def test_converges_towards_a_constant_bias(self):
        trim = 0.0
        for _ in range(8):
            trim = hp.update_trim(trim, self.hours(0.6), self.cfg).trim
        self.assertAlmostEqual(trim, 0.6, delta=0.05)


def tick(
    ts=T(12),
    hour=12,
    plan_mode="hold",
    season_on=True,
    calling=False,
    air=21.5,
    target=21.5,
    lower=19.0,
    bias=None,
    fallback=False,
    dt_s=300.0,
):
    return hp.TickSample(
        ts, hour, dt_s, plan_mode, season_on, calling, air, target, lower, bias, fallback
    )


class ZoneDayTests(unittest.TestCase):
    def test_hours_per_mode_and_call_hours(self):
        d = hp.ZoneDay("2026-10-17")
        for _ in range(12):
            d.add_tick(tick(calling=True))
        for _ in range(6):
            d.add_tick(tick(plan_mode="sun_cut"))
        rec = d.record("family", 0.5, hp.TrimUpdate(0.5, None, 0, False))
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
        rec = d.record("family", 0.5, hp.TrimUpdate(0.5, None, 0, False))
        self.assertEqual((rec["band_ticks"], rec["in_band_pct"]), (2, 50.0))

    def test_air_range_and_below_lower_limit(self):
        d = hp.ZoneDay("2026-10-17")
        for air in (20.0, 18.5, 22.0, None):
            d.add_tick(tick(air=air))
        d.add_tick(tick(plan_mode="settle", air=17.0))
        rec = d.record("family", 0.5, hp.TrimUpdate(0.5, None, 0, False))
        self.assertEqual((rec["air_min"], rec["air_max"]), (17.0, 22.0))
        self.assertEqual(rec["below_lower_ticks"], 1)
        self.assertEqual(rec["air_mean"], round((20.0 + 18.5 + 22.0 + 17.0) / 4, 2))

    def test_afternoon_excess_tracks_the_overheating_the_cut_aims_at(self):
        d = hp.ZoneDay("2026-10-17")
        d.add_tick(tick(hour=9, air=25.0, target=21.5))
        d.add_tick(tick(hour=14, air=22.4, target=21.5))
        d.add_tick(tick(hour=15, air=22.9, target=21.5))
        rec = d.record("family", 0.5, hp.TrimUpdate(0.5, None, 0, False))
        self.assertEqual(rec["afternoon_excess_max"], 1.4)

    def test_bias_goes_into_the_trim_hours(self):
        d = hp.ZoneDay("2026-10-17")
        d.add_tick(tick(hour=8, bias=0.4))
        d.add_tick(tick(hour=8, bias=0.6))
        d.add_tick(tick(hour=3, bias=9.0))
        d.add_tick(tick(hour=9, bias=None))
        self.assertEqual(d.bias, {8: [1.0, 2]})

    def test_fallback_hours(self):
        d = hp.ZoneDay("2026-10-17")
        d.add_tick(tick(fallback=True))
        d.add_tick(tick())
        self.assertEqual(d.record("z", 0, hp.TrimUpdate(0, None, 0, False))["fallback_hours"], 0.08)

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

    def test_budget_is_noted_once_and_the_record_carries_the_trim_update(self):
        d = hp.ZoneDay("2026-10-17")
        d.note_budget({"h_pred": 4.0})
        d.note_budget({"h_pred": 9.0})
        rec = d.record("family", 0.5, hp.TrimUpdate(0.55, 0.6, 9, True))
        self.assertEqual(rec["budget"], {"h_pred": 4.0})
        self.assertEqual(
            (rec["trim_before"], rec["trim_after"], rec["trim_bias"], rec["trim_valid_hours"]),
            (0.5, 0.55, 0.6, 9),
        )
        self.assertEqual((rec["type"], rec["zone"], rec["day"]), ("daily", "family", "2026-10-17"))

    def test_state_survives_a_json_round_trip(self):
        import json

        d = hp.ZoneDay("2026-10-17")
        for h in (8, 9, 14):
            d.add_tick(tick(hour=h, bias=0.4, calling=True, air=22.0 + h / 100))
        d.add_would_write(T(10), 22.0)
        d.note_budget({"h_pred": 4.0})
        again = hp.ZoneDay("2026-10-17", json.loads(json.dumps(d.snapshot())))
        self.assertEqual(
            again.record("z", 0, hp.TrimUpdate(0, None, 0, False)),
            d.record("z", 0, hp.TrimUpdate(0, None, 0, False)),
        )
        self.assertEqual(again.bias, d.bias)


if __name__ == "__main__":
    unittest.main()
