from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
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

import smart_cooling as sc  # noqa: E402
import climate_model as cm  # noqa: E402


def make_app(**overrides):
    """SmartCooling instance without running AppDaemon's initialize() -
    the pure helpers only read a handful of instance attributes."""
    app = sc.SmartCooling.__new__(sc.SmartCooling)
    app.min_temp = overrides.get("min_temp", 16.0)
    app._rise_frac = overrides.get("rise_frac", 0.5)
    app._rise_samples = overrides.get("rise_samples", 10)
    app.dry_run = overrides.get("dry_run", False)
    app.stall_deficit_min = overrides.get("stall_deficit_min", 0.3)
    app.stall_burp_cooldown_min = overrides.get("stall_burp_cooldown_min", 15)
    app._burp_until = overrides.get("burp_until", None)
    app._last_burp = overrides.get("last_burp", None)
    app.sat_engaged_min = overrides.get("sat_engaged_min", 90)
    app.sat_reset_rise = overrides.get("sat_reset_rise", 0.5)
    app.feasible_min_samples = overrides.get("feasible_min_samples", 2)
    app._sat_min = overrides.get("sat_min", None)
    app._sat_noprog_min = 0.0
    app._saturated = False
    app._feasible_floor = overrides.get("feasible_floor", None)
    app._feasible_samples = overrides.get("feasible_samples", 0)
    app._dry_min = overrides.get("dry_min", 0.0)
    # learned cooling rate (C/h) + scheduler commitment slack
    app.floor_cool_cph = overrides.get("floor_cool_cph", 1.0)
    app._cool_cph = overrides.get("cool_cph", None)
    app._cool_cph_samples = overrides.get("cool_cph_samples", 0)
    app._rate_ref_floor = overrides.get("rate_ref_floor", None)
    app._rate_engaged_min = overrides.get("rate_engaged_min", 0.0)
    app.cool_cph_min = overrides.get("cool_cph_min", 0.3)
    app.cool_cph_max = overrides.get("cool_cph_max", 4.0)
    app.cool_rate_min_engaged = overrides.get("cool_rate_min_engaged", 20.0)
    # per-night achieved-depth learning (see _finalize_night)
    app.feasible_learn_min_engaged = overrides.get("feasible_learn_min_engaged", 120.0)
    app.feasible_probe_c = overrides.get("feasible_probe_c", 1.0)
    app.rate_learn_min_headroom = overrides.get("rate_learn_min_headroom", 0.7)
    app.crawl_rate_cph = overrides.get("crawl_rate_cph", 0.4)
    app.ground_actuation = overrides.get("ground_actuation", True)
    app.vent_tau_h = overrides.get("vent_tau_h", 7.0)
    app.curtain_entity = "cover.bedroom_blind"
    app.curtain_open_max_pos = 45.0
    app.bedroom_window_name = "bedroom"
    app.vent_tau_indirect_h = overrides.get("vent_tau_indirect_h", 14.0)
    app.bedroom_door_entity = "binary_sensor.bedroom_door_contact"
    app.vent_tau_door_h = overrides.get("vent_tau_door_h", 10.0)
    app._last_ground_logged = None
    app._plan_rec_prev = None
    app._plan_rec_since = None
    # wake display knobs (resolve_wake inputs; display-only)
    app.alarm_time_entity = "input_datetime.wakeup_bedroom"
    app.alarm_enabled_entity = "input_boolean.wakeup_bedroom"
    app.fallback_workday = "07:00:00"
    app.fallback_weekend = "09:00:00"
    app._last_wake_floor = overrides.get("last_wake_floor", None)
    app._night_floor_min = overrides.get("night_floor_min", None)
    app._night_engaged_min = overrides.get("night_engaged_min", 0.0)
    app._learned_tonight = overrides.get("learned_tonight", False)
    app.commit_price_margin = overrides.get("commit_price_margin", 0.15)
    app.sleep_hours = overrides.get("sleep_hours", 8.0)
    app.cool_kw = overrides.get("cool_kw", 0.5)          # _schedule's est-cost term
    # weather-model attributes (Model D coefficients + memory + shadow flags)
    app.person_offset = overrides.get("person_offset", 0.5)
    app.weather_model_enabled = overrides.get("weather_model_enabled", True)
    app.wm_shadow = overrides.get("wm_shadow", False)
    app.wm_forecast_reuse_h = overrides.get("wm_forecast_reuse_h", 6.0)
    app.wm_b0 = overrides.get("wm_b0", 15.797)
    app.wm_b_solar = overrides.get("wm_b_solar", 0.0162)
    app.wm_b_vent = overrides.get("wm_b_vent", 0.198)
    app.wm_vent_knee = overrides.get("wm_vent_knee", 24.0)
    app.wm_b_prev = overrides.get("wm_b_prev", 0.287)
    app.wm_safety_margin = overrides.get("wm_safety_margin", 0.0)
    app.wm_nowcast_relief = overrides.get("wm_nowcast_relief", 1.5)
    app.wm_clearsky_peak = overrides.get("wm_clearsky_peak", 700.0)
    app.wm_cloud_atten = overrides.get("wm_cloud_atten", 0.75)
    app.wm_peak_hour = overrides.get("wm_peak_hour", 15)
    app.solar_sensor = overrides.get("solar_sensor", "sensor.gw2000a_solar_radiation")
    app._prev_kitchen_max = overrides.get("prev_kitchen_max", None)
    app._kitchen_max_today = overrides.get("kitchen_max_today", None)
    app._kitchen_max_date = overrides.get("kitchen_max_date", None)
    app._fc_cache = None
    app._fc_cache_at = None
    app._fc_fail_at = None
    app._fc_warned_at = None
    app._fc_served_age_min = None
    # live session-cost metering (see _track_session_cost / _finalize_session /
    # _status_base) + the sleep-plan estimator's calibration knobs
    app.ac_energy_entity = overrides.get("ac_energy_entity", "sensor.ac_plug_energy")
    app.session_energy_factor = overrides.get("session_energy_factor", 2.5)
    app.est_price_slots = overrides.get("est_price_slots", 8)
    app._session_kwh0 = overrides.get("session_kwh0", None)
    app._session_last_counter = overrides.get("session_last_counter", None)
    app._session_kwh = overrides.get("session_kwh", 0.0)
    app._session_cost = overrides.get("session_cost", 0.0)
    app._last_session_kwh = overrides.get("last_session_kwh", None)
    app._last_session_cost = overrides.get("last_session_cost", None)
    app._night_cost_ema = overrides.get("night_cost_ema", None)
    app._night_cost_samples = overrides.get("night_cost_samples", 0)
    # kwh_per_deg scaling model (2026-07-29 rebuild -- replaces the flat night_cost_ema
    # preference in plan_sleep) + the trivial-session guard + the sleep plan's own stashed
    # verdict _track_session_cost reads the pre-cool deficit from at session start.
    app.session_min_kwh = overrides.get("session_min_kwh", 0.5)
    app._session_deficit0 = overrides.get("session_deficit0", None)
    app._session_started_at = overrides.get("session_started_at", None)
    app._kwh_per_deg = overrides.get("kwh_per_deg", 1.6)
    app._kwh_per_deg_samples = overrides.get("kwh_per_deg_samples", 0)
    app._last_plan = overrides.get("last_plan", None)
    # day-history for the dashboard bar (see _track_cool_intervals / _publish)
    app._cool_intervals = overrides.get("cool_intervals", [])
    app._last_pub_status = overrides.get("last_pub_status", None)
    app._save_state = lambda: None
    app.log = lambda *a, **k: None
    return app


class AttrsBuild(unittest.TestCase):
    """Regression test for the 2026-07-14 incident: `_attrs()` referenced
    `ceiling_base` as a free variable instead of a parameter, so EVERY armed
    evaluation crashed with NameError and SmartCooling silently never
    published anything but the disarmed status - "not ready to be used" with
    no visible error to the user. Calling `_attrs()` at all is the trip wire:
    the bug fired on the very first reference, independent of arguments."""

    def _call(self, ceiling, ceiling_base):
        app = make_app()
        return app._attrs(
            floor=22.0, mid=22.5, zone=22.2, ceil_s=21.0, ac_s=17.0,
            bath=24.0, kitchen=23.0, E=24.0, target=21.5, deficit=0.5,
            ceiling=ceiling, price_now=1.2, window_open=True,
            run_min=45, next_start=datetime(2026, 7, 14, 22, 15), est_cost=1.8,
            floor_limited=False, ceiling_base=ceiling_base,
        )

    def test_no_nameerror_and_keys_present(self):
        attrs = self._call(ceiling=22.0, ceiling_base=23.0)
        for key in ("ceiling_base", "ceiling_source", "night_ceiling", "floor_target"):
            self.assertIn(key, attrs)

    def test_ceiling_source_comfort_layer_when_lowered(self):
        attrs = self._call(ceiling=22.0, ceiling_base=23.0)
        self.assertEqual(attrs["ceiling_source"], "comfort layer")
        self.assertEqual(attrs["ceiling_base"], 23.0)

    def test_ceiling_source_knob_when_unadjusted(self):
        attrs = self._call(ceiling=23.0, ceiling_base=23.0)
        self.assertEqual(attrs["ceiling_source"], "knob")


class StatusBaseRiseFrac(unittest.TestCase):
    """rise_frac (+ rise_samples) must ride on EVERY status publish, not only the armed
    _attrs path. The disarmed / not-deployed / no_data branches publish with replace=True,
    so without _status_base() they'd WIPE rise_frac from sensor.smart_cooling_status on
    every daytime/disarmed tick -- bedroom_comfort's passthrough would then fall back to its
    0.5 default despite a learned ~0.7."""

    def test_status_base_carries_learned_rise_frac(self):
        base = make_app(rise_frac=0.71, rise_samples=6)._status_base()
        self.assertEqual(base["rise_frac"], 0.71)
        self.assertEqual(base["rise_samples"], 6)

    def test_status_base_matches_armed_attrs_value(self):
        # the disarmed publishes must agree with the armed _attrs on the same number
        app = make_app(rise_frac=0.68, rise_samples=4)
        attrs = app._attrs(
            floor=22.0, mid=22.5, zone=22.2, ceil_s=21.0, ac_s=17.0,
            bath=24.0, kitchen=23.0, E=24.0, target=21.5, deficit=0.5,
            ceiling=22.0, price_now=1.2, window_open=True, run_min=45,
            next_start=None, est_cost=1.8, floor_limited=False, ceiling_base=23.0)
        base = app._status_base()
        self.assertEqual(base["rise_frac"], attrs["rise_frac"])
        self.assertEqual(base["rise_samples"], attrs["rise_samples"])

    def test_merges_over_wm_dbg_without_key_collision(self):
        # the real call sites spread {..., **self._status_base(), **wm_dbg}; the learned
        # keys and the weather-debug keys are disjoint, so neither clobbers the other.
        app = make_app(rise_frac=0.7, rise_samples=3)
        wm_dbg = {"equilibrium_weather": None, "equilibrium_legacy": 24.0}
        merged = {"deployed": True, **app._status_base(), **wm_dbg}
        self.assertEqual(merged["rise_frac"], 0.7)
        self.assertEqual(merged["rise_samples"], 3)
        self.assertEqual(merged["equilibrium_legacy"], 24.0)


class NextMidnight(unittest.TestCase):
    """Regression coverage for the 2026-07-15 bedtime-knob removal: the price-optimizer's
    search deadline is now a fixed midnight hard cap (user: "we should not count that we can
    cool past midnight"), replacing the old per-night bedtime_entity lookup."""

    def _midnight(self, now):
        app = make_app()
        return app._next_midnight(now)

    def test_midday_rolls_to_tonight(self):
        now = datetime(2026, 7, 15, 14, 0)
        self.assertEqual(self._midnight(now), datetime(2026, 7, 16, 0, 0))

    def test_just_before_midnight_still_tonight(self):
        now = datetime(2026, 7, 15, 23, 59)
        self.assertEqual(self._midnight(now), datetime(2026, 7, 16, 0, 0))

    def test_already_past_midnight_rolls_to_tomorrow_not_negative(self):
        # if _evaluate somehow runs at 02:00, the deadline must still be STRICTLY future -
        # using "start of today" here would put it 2 hours in the past.
        now = datetime(2026, 7, 15, 2, 0)
        deadline = self._midnight(now)
        self.assertEqual(deadline, datetime(2026, 7, 16, 0, 0))
        self.assertGreater(deadline, now)


class Deadline(unittest.TestCase):
    """Regression coverage for the 2026-07-15 23:52 shutdown: with the raw midnight cap,
    _schedule's slot count hit zero just before midnight and the AC went off with a live
    deficit while the user was still up (bedtime varies 22:00-01:00). The deadline now
    keeps a >=1h horizon at the day's edge and rolls a 1h maintenance window through the
    night (00-06) until the AC-removed press."""

    def _deadline(self, now):
        app = make_app()
        return app._deadline(now)

    def test_daytime_is_plain_midnight(self):
        now = datetime(2026, 7, 15, 14, 0)
        self.assertEqual(self._deadline(now), datetime(2026, 7, 16, 0, 0))

    def test_2352_keeps_an_hour_not_zero_slots(self):
        # the exact failure minute from 2026-07-15
        now = datetime(2026, 7, 15, 23, 52)
        self.assertEqual(self._deadline(now), datetime(2026, 7, 16, 0, 52))

    def test_past_midnight_rolls_maintenance_window(self):
        now = datetime(2026, 7, 16, 0, 30)
        self.assertEqual(self._deadline(now), datetime(2026, 7, 16, 1, 30))

    def test_early_morning_still_maintenance(self):
        now = datetime(2026, 7, 16, 5, 59)
        self.assertEqual(self._deadline(now), datetime(2026, 7, 16, 6, 59))

    def test_after_six_back_to_tonights_midnight(self):
        now = datetime(2026, 7, 16, 6, 1)
        self.assertEqual(self._deadline(now), datetime(2026, 7, 17, 0, 0))

    def test_2352_schedule_cools_now_on_deficit(self):
        # end-to-end through _schedule: at 23:52 with 90 min still needed and flat
        # prices, the old deadline meant total=0 slots -> AC off; now it must cool.
        app = make_app()
        app.cool_kw = 0.5
        now = datetime(2026, 7, 15, 23, 52)
        cool_now, _, run_min, _, _ = app._schedule(
            now, app._deadline(now), 90, lambda dt: 1.97)
        self.assertTrue(cool_now)
        self.assertGreater(run_min, 0)


class PlanDeadline(unittest.TestCase):
    """Two-tier horizon (user, 2026-07-16): slots after 22:00 are bonus, not guaranteed --
    bedtime spans 22:00-01:00 and the AC-removed press can erase them, so the load-bearing
    plan must fit before 22:00. The regression that motivated it: with the plain midnight
    horizon, a 21:1x evening top-up got scheduled to START at 22:08 -- a machine spinning up
    right as the user might be getting into bed."""

    def _plan(self, now):
        app = make_app()
        return app._plan_deadline(now)

    def test_daytime_caps_at_2200_not_midnight(self):
        now = datetime(2026, 7, 16, 14, 0)
        self.assertEqual(self._plan(now), datetime(2026, 7, 16, 22, 0))

    def test_evening_2113_still_2200(self):
        # the exact situation that motivated the tier: work must land NOW, not at 22:08
        now = datetime(2026, 7, 16, 21, 13)
        self.assertEqual(self._plan(now), datetime(2026, 7, 16, 22, 0))

    def test_final_minutes_keep_a_slot_not_zero(self):
        # at 21:52 a hard 22:00 cap would leave zero slots and strand a live deficit
        # for 8 minutes; the 15-min floor keeps cooling through the tier boundary.
        now = datetime(2026, 7, 16, 21, 52)
        self.assertEqual(self._plan(now), datetime(2026, 7, 16, 22, 7))

    def test_after_2200_is_bonus_tier_midnight(self):
        now = datetime(2026, 7, 16, 22, 30)
        self.assertEqual(self._plan(now), datetime(2026, 7, 17, 0, 0))

    def test_overnight_delegates_to_maintenance_window(self):
        now = datetime(2026, 7, 17, 0, 30)
        self.assertEqual(self._plan(now), datetime(2026, 7, 17, 1, 30))

    def test_2113_schedule_cools_now_when_work_exceeds_remaining_slots(self):
        # 48 min of work, 47 min of guaranteed horizon -> need >= total -> cool immediately
        # (the old midnight horizon deferred this exact case to the 22:08+ cheap shoulder).
        app = make_app()
        app.cool_kw = 0.5
        now = datetime(2026, 7, 16, 21, 13)
        prices = {21: 2.8, 22: 2.3, 23: 2.05}
        cool_now, _, _, _, _ = app._schedule(
            now, app._plan_deadline(now), 48, lambda dt: prices.get(dt.hour, 2.5))
        self.assertTrue(cool_now)


class StashLightout(unittest.TestCase):
    """The AC-removed toggle is now the lights-out trigger (was: a bedtime time-window guess
    that could be hours off from when anyone actually went to bed, corrupting rise_frac)."""

    def test_stash_records_baseline_and_end_time(self):
        app = make_app()
        app.sleep_hours = 8.0
        app._save_state = lambda: None
        app.log = lambda *a, **k: None
        now = datetime(2026, 7, 15, 23, 30)

        app._stash_lightout(floor=22.3, E=25.1, now=now)

        self.assertEqual(app._lightout["date"], "2026-07-15")
        self.assertEqual(app._lightout["F0"], 22.3)
        self.assertEqual(app._lightout["E"], 25.1)
        self.assertEqual(app._lightout["end"], datetime(2026, 7, 16, 7, 30).isoformat())

    def test_stash_overwrites_stale_inflight_record(self):
        # a second press the same night (changed their mind, went back to bed later) should
        # replace the first baseline, not merge with or defer to it.
        app = make_app()
        app.sleep_hours = 8.0
        app._save_state = lambda: None
        app.log = lambda *a, **k: None
        app._lightout = {"date": "2026-07-15", "F0": 23.0, "E": 24.0, "end": "stale"}

        app._stash_lightout(floor=21.0, E=25.0, now=datetime(2026, 7, 16, 0, 15))

        self.assertEqual(app._lightout["F0"], 21.0)
        self.assertEqual(app._lightout["E"], 25.0)


class SessionCostTracking(unittest.TestCase):
    """Live session-cost metering (validated 2026-07-21: the sleep-plan's AC estimate was
    4-8x too LOW against the Shelly meter). _track_session_cost meters every tick from the
    Shelly cumulative kWh counter; _finalize_session freezes a session into the learned
    night_cost_ema plan_sleep prefers over the theoretical estimate."""

    def test_noop_when_counter_none(self):
        app = make_app()
        app._track_session_cost(True, 1.5, None)
        self.assertIsNone(app._session_kwh0)
        self.assertEqual(app._session_kwh, 0.0)
        self.assertEqual(app._session_cost, 0.0)

    def test_session_starts_on_deployed(self):
        app = make_app()
        app._track_session_cost(True, 1.5, 100.0)
        self.assertEqual(app._session_kwh0, 100.0)
        self.assertEqual(app._session_last_counter, 100.0)
        self.assertEqual(app._session_kwh, 0.0)
        self.assertEqual(app._session_cost, 0.0)

    def test_no_session_starts_while_not_deployed(self):
        # the unit isn't reachable -> nothing to meter yet, even with a counter reading
        # (e.g. a stale cached state).
        app = make_app()
        app._track_session_cost(False, 1.5, 100.0)
        self.assertIsNone(app._session_kwh0)

    def test_session_start_captures_deficit0_from_last_plan(self):
        # kwh_per_deg learning anchor (2026-07-29): the deficit the sleep plan last
        # published, stashed the moment the session opens.
        app = make_app(last_plan={"rec": "ac", "deficit": 3.2})
        app._track_session_cost(True, 1.5, 100.0)
        self.assertEqual(app._session_deficit0, 3.2)

    def test_session_start_with_no_plan_yet_leaves_deficit0_none(self):
        app = make_app(last_plan=None)
        app._track_session_cost(True, 1.5, 100.0)
        self.assertIsNone(app._session_deficit0)

    def test_deficit0_is_not_recaptured_mid_session(self):
        # only the session-START tick reads _last_plan -- later ticks (the plan re-
        # publishes every ~15 min) must not overwrite the anchor mid-session.
        app = make_app(last_plan={"deficit": 3.2})
        app._track_session_cost(True, 1.5, 100.0)      # session starts
        app._last_plan = {"deficit": 0.1}               # plan re-published since
        app._track_session_cost(True, 1.5, 100.5)       # same open session, not a new start
        self.assertEqual(app._session_deficit0, 3.2)

    def test_accumulates_kwh_and_cost_with_price(self):
        app = make_app()
        app._track_session_cost(True, 1.5, 100.0)     # start
        app._track_session_cost(True, 2.0, 100.5)     # +0.5 kWh at 2.0 kr/kWh
        self.assertAlmostEqual(app._session_kwh, 0.5)
        self.assertAlmostEqual(app._session_cost, 1.0)
        self.assertEqual(app._session_last_counter, 100.5)

    def test_none_price_falls_back_to_1_7(self):
        app = make_app()
        app._track_session_cost(True, 1.5, 100.0)     # start
        app._track_session_cost(True, None, 101.0)    # +1.0 kWh, price missing -> 1.7 fallback
        self.assertAlmostEqual(app._session_kwh, 1.0)
        self.assertAlmostEqual(app._session_cost, 1.7)

    def test_counter_reset_rebaselines_without_going_negative(self):
        # e.g. a Shelly reboot resets its cumulative counter -- must not subtract energy
        # that was never actually used.
        app = make_app()
        app._track_session_cost(True, 1.5, 100.0)
        app._track_session_cost(True, 1.5, 100.5)     # normal +0.5 kWh
        app._track_session_cost(True, 1.5, 0.2)       # counter reset
        self.assertAlmostEqual(app._session_kwh, 0.5)  # unchanged -- no negative delta applied
        self.assertAlmostEqual(app._session_cost, 0.75)
        self.assertEqual(app._session_last_counter, 0.2)  # re-baselined to the new reading

    def test_flapping_deploy_keeps_metering_an_open_session(self):
        # deployed goes False mid-session (the unit briefly drops off cloud) -- metering
        # must keep accumulating against the still-open session, not stop or restart it.
        app = make_app()
        app._track_session_cost(True, 1.5, 100.0)
        app._track_session_cost(False, 1.5, 101.0)
        self.assertEqual(app._session_kwh0, 100.0)     # unchanged -- no new session started
        self.assertAlmostEqual(app._session_kwh, 1.0)

    def test_finalize_sets_last_and_ema_and_clears_baseline(self):
        app = make_app()
        app._track_session_cost(True, 1.5, 100.0)
        app._track_session_cost(True, 1.5, 102.0)     # 2.0 kWh * 1.5 kr = 3.0 kr
        app._finalize_session()
        self.assertIsNone(app._session_kwh0)
        self.assertIsNone(app._session_last_counter)
        self.assertEqual(app._last_session_kwh, 2.0)
        self.assertEqual(app._last_session_cost, 3.0)
        self.assertEqual(app._night_cost_ema, 3.0)     # first sample -> EMA seeds at the value
        self.assertEqual(app._night_cost_samples, 1)
        # running totals stay visible for display until the next session actually starts
        self.assertEqual(app._session_kwh, 2.0)
        self.assertEqual(app._session_cost, 3.0)

    def test_second_finalize_is_a_noop(self):
        app = make_app()
        app._track_session_cost(True, 1.5, 100.0)
        app._track_session_cost(True, 1.5, 102.0)
        app._finalize_session()
        app._finalize_session()    # no session open -> must not touch anything again
        self.assertEqual(app._night_cost_samples, 1)
        self.assertEqual(app._last_session_cost, 3.0)

    def test_finalize_without_a_session_is_a_noop(self):
        app = make_app()
        app._finalize_session()
        self.assertIsNone(app._last_session_cost)
        self.assertEqual(app._night_cost_samples, 0)

    def test_ema_blends_across_nights_with_0_6_0_4_weights(self):
        app = make_app(night_cost_ema=4.0, night_cost_samples=3)
        app._track_session_cost(True, 2.0, 0.0)
        app._track_session_cost(True, 2.0, 1.0)        # 1.0 kWh * 2.0 kr = 2.0 kr
        app._finalize_session()
        self.assertAlmostEqual(app._night_cost_ema, 0.6 * 4.0 + 0.4 * 2.0, places=2)  # 3.2
        self.assertEqual(app._night_cost_samples, 4)

    # The disarmed+undeployed fallback close-out (user unplugged without pressing "AC
    # removed") lives inside _evaluate_locked, not on _track_session_cost/_finalize_session
    # directly -- it's exercised end-to-end via the real wiring in
    # EvaluateTickWiring.test_disarmed_undeployed_finalizes_a_dangling_session instead of
    # duplicating that class's full evaluate-loop stub scaffolding here.


class KwhPerDegLearning(unittest.TestCase):
    """2026-07-29 cost-model rebuild: kwh_per_deg (kWh spent per degree C of pre-cool
    deficit closed) replaces the flat night_cost_ema EMA plan_sleep used to prefer -- that
    EMA was poisoned by 3 finalized 0.00 kWh sessions (armed+deployed nights the compressor
    never actually ran) dragging it to 0.36 kr against a real ~4.50 kr metered average.
    session_min_kwh excludes those trivial sessions from EVERY learned value; kwh_per_deg
    itself is learned from session_kwh / the deficit captured at session start (see
    SessionCostTracking.test_session_start_captures_deficit0_from_last_plan)."""

    def _run_session(self, app, kwh, price=1.5, deficit0=None):
        """Open + accumulate exactly `kwh` kWh against a fresh session, then finalize."""
        if deficit0 is not None:
            app._last_plan = {"deficit": deficit0}
        app._track_session_cost(True, price, 100.0)
        app._track_session_cost(True, price, 100.0 + kwh)
        app._finalize_session()

    def test_trivial_session_is_skipped_entirely(self):
        # 0.1 kWh < session_min_kwh (0.5 default) -- a night the compressor barely/never
        # ran. deficit0 is deliberately large enough to otherwise qualify, proving the skip
        # is driven by session_min_kwh, not the deficit gate.
        app = make_app(night_cost_ema=4.0, night_cost_samples=2,
                       kwh_per_deg=1.8, kwh_per_deg_samples=3)
        self._run_session(app, kwh=0.1, deficit0=3.0)
        self.assertIsNone(app._session_kwh0)          # baseline still clears
        self.assertEqual(app._last_session_kwh, 0.1)  # _last_session_* still set
        # nothing learned: every value unchanged from its pre-session seed
        self.assertEqual(app._night_cost_ema, 4.0)
        self.assertEqual(app._night_cost_samples, 2)
        self.assertEqual(app._kwh_per_deg, 1.8)
        self.assertEqual(app._kwh_per_deg_samples, 3)

    def test_trivial_session_is_logged_as_skipped(self):
        app = make_app()
        logged = []
        app.log = lambda *a, **k: logged.append(a)
        self._run_session(app, kwh=0.1, deficit0=3.0)
        self.assertTrue(any("skip" in str(a).lower() for a in logged))

    def test_qualifying_session_first_sample_seeds_kwh_per_deg(self):
        app = make_app(kwh_per_deg_samples=0)
        self._run_session(app, kwh=3.0, deficit0=2.0)   # obs = 3.0 / 2.0 = 1.5
        self.assertAlmostEqual(app._kwh_per_deg, 1.5, places=6)
        self.assertEqual(app._kwh_per_deg_samples, 1)

    def test_qualifying_session_blends_subsequent_samples(self):
        app = make_app(kwh_per_deg=1.6, kwh_per_deg_samples=1)
        self._run_session(app, kwh=3.0, deficit0=1.0)   # obs = 3.0 / 1.0 = 3.0
        self.assertAlmostEqual(app._kwh_per_deg, 0.6 * 1.6 + 0.4 * 3.0, places=6)  # 2.16
        self.assertEqual(app._kwh_per_deg_samples, 2)

    def test_deficit_exactly_at_threshold_qualifies(self):
        app = make_app(kwh_per_deg_samples=0)
        self._run_session(app, kwh=1.0, deficit0=0.5)   # obs = 1.0 / 0.5 = 2.0
        self.assertAlmostEqual(app._kwh_per_deg, 2.0, places=6)
        self.assertEqual(app._kwh_per_deg_samples, 1)

    def test_missing_deficit_skips_kwh_per_deg_but_still_learns_night_cost_ema(self):
        # no plan had published a deficit when the session opened -- can't attribute the
        # energy to a degree of deficit, so kwh_per_deg is skipped, but night_cost_ema
        # (independent of deficit) still EMAs normally.
        app = make_app(kwh_per_deg=1.6, kwh_per_deg_samples=1,
                       night_cost_ema=4.0, night_cost_samples=2)
        self._run_session(app, kwh=3.0, deficit0=None)
        self.assertEqual(app._kwh_per_deg, 1.6)
        self.assertEqual(app._kwh_per_deg_samples, 1)
        self.assertNotEqual(app._night_cost_ema, 4.0)

    def test_shallow_deficit_skips_kwh_per_deg_but_still_learns_night_cost_ema(self):
        app = make_app(kwh_per_deg=1.6, kwh_per_deg_samples=1,
                       night_cost_ema=4.0, night_cost_samples=2)
        self._run_session(app, kwh=3.0, deficit0=0.3)   # under the 0.5C attribution floor
        self.assertEqual(app._kwh_per_deg, 1.6)
        self.assertEqual(app._kwh_per_deg_samples, 1)
        self.assertNotEqual(app._night_cost_ema, 4.0)


class ShouldBurp(unittest.TestCase):
    """Stall-breaker decision (2026-07-15): the unit parks at ~300 W 'idle' with real floor
    deficit left because its intake sensor sits in its own cold outflow. Measured: no fan
    mode wakes it; a fan_only burp does (44 W spent, 550-800 W of real cooling resumes)."""

    NOW = datetime(2026, 7, 15, 15, 30)

    def test_burps_when_idle_with_deficit(self):
        app = make_app()
        self.assertTrue(app._should_burp("idle", "cool", 4.0, self.NOW))

    def test_no_burp_while_actively_cooling(self):
        app = make_app()
        self.assertFalse(app._should_burp("cooling", "cool", 4.0, self.NOW))

    def test_no_burp_when_mode_not_cool(self):
        # fan_only during a burp also reports a non-cooling action; must not re-trigger
        app = make_app()
        self.assertFalse(app._should_burp("fan", "fan_only", 4.0, self.NOW))
        self.assertFalse(app._should_burp("idle", "off", 4.0, self.NOW))

    def test_no_burp_when_target_basically_reached(self):
        app = make_app()
        self.assertFalse(app._should_burp("idle", "cool", 0.2, self.NOW))

    def test_cooldown_blocks_backtoback_burps(self):
        app = make_app(last_burp=datetime(2026, 7, 15, 15, 20))  # 10 min ago < 15 cooldown
        self.assertFalse(app._should_burp("idle", "cool", 4.0, self.NOW))
        app2 = make_app(last_burp=datetime(2026, 7, 15, 15, 10))  # 20 min ago -> allowed
        self.assertTrue(app2._should_burp("idle", "cool", 4.0, self.NOW))

    def test_no_burp_while_one_is_in_flight(self):
        app = make_app(burp_until=datetime(2026, 7, 15, 15, 32))
        self.assertFalse(app._should_burp("idle", "cool", 4.0, self.NOW))


class TrackProgress(unittest.TestCase):
    """Feasibility detector (user, 2026-07-15): sustained engaged minutes with zero floor
    progress = the floor won't take more cold tonight. Without this, an unreachable ideal
    target inflates minutes_needed until the scheduler goes time-constrained and pays
    evening-peak prices chasing it."""

    def test_progress_resets_the_clock(self):
        app = make_app()
        app._track_progress(20.0, 0)
        app._track_progress(20.0, 60)             # close to threshold...
        self.assertFalse(app._track_progress(19.8, 45))  # ...but the floor moved: reset
        self.assertEqual(app._sat_noprog_min, 0.0)
        self.assertEqual(app._sat_min, 19.8)

    def test_saturates_after_engaged_minutes_without_progress(self):
        app = make_app()
        app._track_progress(20.0, 0)
        self.assertFalse(app._track_progress(20.0, 45))
        self.assertTrue(app._track_progress(20.1, 45))   # 90 engaged min, no new low
        self.assertEqual(app._feasible_floor, 20.0)
        self.assertEqual(app._feasible_samples, 1)

    def test_coast_time_is_not_evidence(self):
        # AC held off between cheap slots for hours: engaged=0 must never saturate
        app = make_app()
        app._track_progress(20.0, 0)
        for _ in range(20):
            self.assertFalse(app._track_progress(20.2, 0))

    def test_warming_well_past_the_low_resets(self):
        app = make_app()
        app._track_progress(20.0, 0)
        app._track_progress(20.0, 80)
        self.assertFalse(app._track_progress(20.6, 15))  # rose > sat_reset_rise: new situation
        self.assertEqual(app._sat_min, 20.6)
        self.assertEqual(app._sat_noprog_min, 0.0)

    def test_learned_feasible_blends_across_nights(self):
        app = make_app(feasible_floor=19.0, feasible_samples=1)
        app._sat_noprog_min = 90.0
        app._learn_feasible(20.0)
        self.assertEqual(app._feasible_samples, 2)
        self.assertAlmostEqual(app._feasible_floor, 19.5, places=2)  # w=1/2 EMA


class ReachTarget(unittest.TestCase):
    """Post-midnight bonus (user, 2026-07-17): "keep cooling if it make the room more
    comfortable after 00:00, energy is always cheaper then." Past midnight the aim widens
    from the physics-minimum target to the hardware floor -- sealing is imminent (no
    daytime decay to out-leak the extra depth) and the hour is reliably cheap -- bounded
    only by tonight's REAL saturation, never by the historical feasible-floor cap (that
    cap exists to avoid paying peak prices for depth history says won't hold; at these
    prices trying is nearly free)."""

    def test_daytime_unsaturated_uses_plain_target(self):
        app = make_app()
        now = datetime(2026, 7, 16, 14, 0)
        self.assertEqual(app._reach_target(now, target=19.3, saturated=False), 19.3)

    def test_daytime_respects_historical_feasible_cap(self):
        app = make_app(feasible_floor=20.5, feasible_samples=3)
        now = datetime(2026, 7, 16, 21, 0)
        # ideal 18.3 is below the learned wall (probed feasible_probe_c=1.0 deep) -> capped
        self.assertEqual(app._reach_target(now, target=18.3, saturated=False), 19.5)

    def test_past_midnight_ignores_historical_cap_aims_for_hardware_floor(self):
        # same learned history as above, but past midnight: go for min_temp anyway --
        # trying is cheap here, unlike the peak-price hours the historical cap guards.
        app = make_app(feasible_floor=20.5, feasible_samples=3, min_temp=16.0)
        now = datetime(2026, 7, 17, 1, 0)
        self.assertEqual(app._reach_target(now, target=19.3, saturated=False), 16.0)

    def test_past_midnight_still_respects_tonights_real_saturation(self):
        # physics can't be out-cheaped: if the floor already proved it stops at 20.5
        # TONIGHT, the bonus window doesn't get to ignore that.
        app = make_app(sat_min=20.5)
        now = datetime(2026, 7, 17, 2, 0)
        self.assertEqual(app._reach_target(now, target=18.0, saturated=True), 20.5)

    def test_at_six_reverts_to_daytime_rules(self):
        app = make_app(feasible_floor=20.0, feasible_samples=2)
        now = datetime(2026, 7, 17, 6, 0)
        self.assertEqual(app._reach_target(now, target=18.0, saturated=False), 19.0)


class WindowOpen(unittest.TestCase):
    """Regression for the 2026-07-16 Zigbee flaps: binary_sensor.bathroom_window_contact
    dropped off 5x that day (~70 s unavailable->unknown->on blips, battery full), and the
    old fail-closed read (state == "on") turned each dropout into "Bathroom window closed
    -- open it" with the AC off mid-cheap-slot and a 10-min anti-short-cycle lockout on
    the way back. Only an explicit "off" may read as closed; a genuinely closed window is
    backstopped by the bathroom_delta_max guard within minutes."""

    def test_on_is_open(self):
        self.assertTrue(sc.SmartCooling._window_open("on"))

    def test_off_is_the_only_closed_reading(self):
        self.assertFalse(sc.SmartCooling._window_open("off"))

    def test_unavailable_dropout_does_not_interrupt_cooling(self):
        self.assertTrue(sc.SmartCooling._window_open("unavailable"))

    def test_unknown_dropout_does_not_interrupt_cooling(self):
        self.assertTrue(sc.SmartCooling._window_open("unknown"))

    def test_missing_entity_none_does_not_interrupt_cooling(self):
        self.assertTrue(sc.SmartCooling._window_open(None))


class MaybeDry(unittest.TestCase):
    """Dry-finish gates (user, 2026-07-17): evening at-target holds run `dry` mode while
    the air is damp -- but never before the evening (dried air re-exchanges), never past
    the nightly budget, and never on dry air or a missing dew point."""

    def _app(self, dp, dry_min=0.0, dry_date=None):
        app = make_app()
        app.dry_from_hour = 20
        app.dry_dp = 12.0
        app.dry_max_min = 45.0
        app.comfort_entity = "sensor.bedroom_comfort"
        app._dry_min = dry_min
        app._dry_date = dry_date
        async def _attr(entity, key, default=None):
            return dp
        app._attr = _attr
        return app

    def _run(self, app, now, floor=20.2):
        import asyncio
        return asyncio.run(app._maybe_dry(now, floor))

    def test_afternoon_never_dries(self):
        app = self._app(dp=14.0)
        dry, _ = self._run(app, datetime(2026, 7, 17, 15, 0))
        self.assertFalse(dry)

    def test_evening_damp_air_dries_with_budget_in_reason(self):
        now = datetime(2026, 7, 17, 21, 0)
        app = self._app(dp=13.5, dry_min=15.0, dry_date=now.date())
        dry, reason = self._run(app, now)
        self.assertTrue(dry)
        self.assertIn("13.5", reason)
        self.assertIn("30 more min", reason)

    def test_dry_air_stays_off(self):
        now = datetime(2026, 7, 17, 21, 0)
        app = self._app(dp=9.0, dry_date=now.date())
        dry, _ = self._run(app, now)
        self.assertFalse(dry)

    def test_budget_exhausted_stays_off(self):
        now = datetime(2026, 7, 17, 21, 0)
        app = self._app(dp=14.0, dry_min=45.0, dry_date=now.date())
        dry, _ = self._run(app, now)
        self.assertFalse(dry)

    def test_new_day_resets_budget(self):
        from datetime import date
        now = datetime(2026, 7, 17, 21, 0)
        app = self._app(dp=14.0, dry_min=45.0, dry_date=date(2026, 7, 16))
        dry, _ = self._run(app, now)
        self.assertTrue(dry)
        self.assertEqual(app._dry_min, 0.0)

    def test_missing_dew_point_stays_off(self):
        now = datetime(2026, 7, 17, 21, 0)
        app = self._app(dp=None, dry_date=now.date())
        dry, _ = self._run(app, now)
        self.assertFalse(dry)


class DeployWatchdog(unittest.TestCase):
    """Regression for 2026-07-19: armed Cool night, but the AC's own smart plug had
    lost power since the previous night's unplug (its button caught in the same
    motion) - climate stayed unreachable all afternoon with no signal to the user
    that anything was wrong. One notification once the grace period is exceeded;
    resets on deploy or disarm so a later recurrence can notify again."""

    def _app(self):
        app = make_app()
        app.deploy_watchdog_min = 20.0
        app._not_deployed_since = None
        app._deploy_watchdog_notified = False
        app._notified = []
        async def _notify(message):
            app._notified.append(message)
        app._notify = _notify
        return app

    def _run(self, app, now, master_on, deployed):
        import asyncio
        asyncio.run(app._check_deploy_watchdog(now, master_on, deployed))

    def test_deployed_never_starts_a_streak(self):
        app = self._app()
        self._run(app, datetime(2026, 7, 19, 12, 0), True, True)
        self.assertIsNone(app._not_deployed_since)
        self.assertEqual(app._notified, [])

    def test_disarmed_and_not_deployed_is_normal_storage_no_notify(self):
        app = self._app()
        self._run(app, datetime(2026, 7, 19, 12, 0), False, False)
        self.assertIsNone(app._not_deployed_since)
        self.assertEqual(app._notified, [])

    def test_within_grace_period_stays_quiet(self):
        app = self._app()
        start = datetime(2026, 7, 19, 12, 0)
        self._run(app, start, True, False)
        self._run(app, start + timedelta(minutes=10), True, False)
        self.assertEqual(app._notified, [])

    def test_past_grace_period_notifies_once(self):
        app = self._app()
        start = datetime(2026, 7, 19, 12, 0)
        self._run(app, start, True, False)
        self._run(app, start + timedelta(minutes=21), True, False)
        self._run(app, start + timedelta(minutes=36), True, False)  # still stuck
        self.assertEqual(len(app._notified), 1)
        self.assertIn("plug/switch", app._notified[0])

    def test_recovering_then_failing_again_notifies_a_second_time(self):
        app = self._app()
        start = datetime(2026, 7, 19, 12, 0)
        self._run(app, start, True, False)
        self._run(app, start + timedelta(minutes=25), True, False)
        self.assertEqual(len(app._notified), 1)
        self._run(app, start + timedelta(minutes=26), True, True)   # recovers
        self.assertIsNone(app._not_deployed_since)
        self._run(app, start + timedelta(minutes=30), True, False)  # fails again
        self._run(app, start + timedelta(minutes=51), True, False)
        self.assertEqual(len(app._notified), 2)

    def test_disarming_mid_streak_clears_it_without_notifying(self):
        app = self._app()
        start = datetime(2026, 7, 19, 12, 0)
        self._run(app, start, True, False)
        self._run(app, start + timedelta(minutes=10), False, False)  # disarmed before grace elapses
        self.assertIsNone(app._not_deployed_since)
        self._run(app, start + timedelta(minutes=40), False, False)
        self.assertEqual(app._notified, [])


class CoolingFan(unittest.TestCase):
    """Occupied quiet cooling (user, 2026-07-19: "watching TV in bed -> cool, just
    less noisy"): active cooling drops to the quiet fan speed while someone's in
    bed, same signal as the burp quiet-gate, otherwise the configured cool_fan."""

    def test_empty_room_uses_configured_fan(self):
        self.assertEqual(sc.SmartCooling._cooling_fan("auto", "silent", False), "auto")

    def test_occupied_uses_quiet_fan(self):
        self.assertEqual(sc.SmartCooling._cooling_fan("auto", "silent", True), "silent")

    def test_quiet_fan_is_configurable(self):
        self.assertEqual(sc.SmartCooling._cooling_fan("medium", "low", True), "low")


class VentingImpaired(unittest.TestCase):
    """Regression for the 2026-07-17->18 incident: the old absolute bathroom_hard_max
    (33C) took ~9h to trip. Delta-above-outdoor (user, 2026-07-19: "it is all about how
    warm it is compared to the outside temperature") is the physically-grounded signal
    -- validated against 19 days of real data: legitimate operation (including a night
    that intentionally ran the bathroom into the high 20s) never exceeded +9.9C above
    outdoor, while the incident was already past +11.4C within an hour of onset and hit
    +21.2C at its worst. DELTA_MAX (12.0) mirrors the deployed default."""

    DELTA_MAX = 12.0

    def test_incident_peak_is_impaired(self):
        self.assertTrue(sc.SmartCooling._venting_impaired(39.1, 17.9, self.DELTA_MAX))

    def test_incident_trips_hours_before_the_old_cap_would_have(self):
        # 23:00 the first evening, ~10h before the 39.8C peak: bath alone (32.4C) was
        # still under the old 33C cap, but the delta (+12.7) had already crossed the
        # new threshold -- this guard would have stopped it here, not at sunrise
        self.assertTrue(sc.SmartCooling._venting_impaired(32.4, 19.7, self.DELTA_MAX))

    def test_known_good_pushing_through_night_is_not_impaired(self):
        # 2026-07-15 22:00: bath 28.5C, deliberately warm and fine -- delta only +7.2
        self.assertTrue(28.5 - 21.3 < self.DELTA_MAX)
        self.assertFalse(sc.SmartCooling._venting_impaired(28.5, 21.3, self.DELTA_MAX))

    def test_exactly_at_delta_is_impaired(self):
        self.assertTrue(sc.SmartCooling._venting_impaired(30.0, 18.0, self.DELTA_MAX))

    def test_just_under_delta_is_not_impaired(self):
        self.assertFalse(sc.SmartCooling._venting_impaired(29.9, 18.0, self.DELTA_MAX))

    def test_missing_bath_fails_closed(self):
        self.assertFalse(sc.SmartCooling._venting_impaired(None, 18.0, self.DELTA_MAX))

    def test_missing_outdoor_fails_closed(self):
        self.assertFalse(sc.SmartCooling._venting_impaired(39.8, None, self.DELTA_MAX))

    def test_hot_outdoor_day_with_normal_delta_is_not_impaired(self):
        # the point of a delta, not an absolute: a genuinely hot day shouldn't nuisance-trip
        self.assertFalse(sc.SmartCooling._venting_impaired(35.0, 28.0, self.DELTA_MAX))


class CondenserHazard(unittest.TestCase):
    """Arm-independent wrapper around _venting_impaired: should we force the AC off
    right now regardless of Cool night's arm state? Same 2026-07-17->18 incident -- the
    guard must fire whenever the unit is genuinely running with impaired venting."""

    DELTA_MAX = 12.0

    def test_running_hot_is_a_hazard(self):
        self.assertTrue(sc.SmartCooling._condenser_hazard(True, "cool", 39.8, 17.9, self.DELTA_MAX))

    def test_normal_delta_is_not_a_hazard(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "cool", 28.5, 21.3, self.DELTA_MAX))

    def test_genuinely_off_is_not_a_hazard_even_if_hot(self):
        # nothing to force off - a hot bathroom with the unit already off isn't this guard's job
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "off", 39.8, 17.9, self.DELTA_MAX))

    def test_not_deployed_is_not_a_hazard(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(False, "cool", 39.8, 17.9, self.DELTA_MAX))

    def test_unavailable_climate_is_not_a_hazard(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "unavailable", 39.8, 17.9, self.DELTA_MAX))

    def test_missing_bath_reading_fails_closed(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "cool", None, 17.9, self.DELTA_MAX))

    def test_missing_outdoor_reading_fails_closed(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "cool", 39.8, None, self.DELTA_MAX))

    def test_dry_mode_can_also_be_a_hazard(self):
        # dry still runs the compressor/condenser, not just cool
        self.assertTrue(sc.SmartCooling._condenser_hazard(True, "dry", 39.8, 17.9, self.DELTA_MAX))

    def test_fan_only_can_also_be_a_hazard(self):
        # conservative on purpose: force off rather than assume a burp explains this reading
        self.assertTrue(sc.SmartCooling._condenser_hazard(True, "fan_only", 39.8, 17.9, self.DELTA_MAX))


class ClearskyWm(unittest.TestCase):
    """Clear-sky half-sine irradiance for the remaining-daylight solar estimate: 0 outside
    the [sunrise, sunset] window, peaking at solar noon (the window midpoint)."""

    def test_zero_before_sunrise(self):
        self.assertEqual(sc.SmartCooling._clearsky_wm(4.0, 5.0, 21.0, 700.0), 0.0)

    def test_zero_at_sunrise_and_sunset_edges(self):
        self.assertEqual(sc.SmartCooling._clearsky_wm(5.0, 5.0, 21.0, 700.0), 0.0)
        self.assertEqual(sc.SmartCooling._clearsky_wm(21.0, 5.0, 21.0, 700.0), 0.0)

    def test_zero_after_sunset(self):
        self.assertEqual(sc.SmartCooling._clearsky_wm(22.5, 5.0, 21.0, 700.0), 0.0)

    def test_peak_at_solar_noon(self):
        # window 5..21 -> midpoint 13.0 -> sin(pi/2) = 1 -> full peak
        self.assertAlmostEqual(sc.SmartCooling._clearsky_wm(13.0, 5.0, 21.0, 700.0),
                               700.0, delta=1e-6)

    def test_noon_exceeds_mid_morning(self):
        noon = sc.SmartCooling._clearsky_wm(13.0, 5.0, 21.0, 700.0)
        morning = sc.SmartCooling._clearsky_wm(8.0, 5.0, 21.0, 700.0)
        self.assertGreater(noon, morning)
        self.assertGreater(morning, 0.0)

    def test_degenerate_window_is_zero(self):
        self.assertEqual(sc.SmartCooling._clearsky_wm(12.0, 21.0, 5.0, 700.0), 0.0)


class SolarMeanAssembly(unittest.TestCase):
    """solar_mean = (measured_mean_sofar * elapsed_h + remaining-daylight clear-sky) / 24,
    with cloud attenuation from the forecast (full clear-sky when the forecast is missing =
    conservative/warm)."""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _app(self, measured_mean, sun=(5.0, 21.0), forecast=None, solar_state="123"):
        app = make_app()
        async def _state(e):
            return solar_state
        app._state = _state
        async def _htm(entity, start, end):
            return measured_mean
        app._history_time_mean = _htm
        async def _sun(now):
            return sun
        app._sun_window = _sun
        async def _fc(now):
            return forecast
        app._get_forecast = _fc
        return app

    def test_no_forecast_uses_full_clearsky(self):
        app = self._app(measured_mean=100.0)
        now = datetime(2026, 7, 20, 12, 0)   # elapsed 12h
        sm = self._run(app._solar_mean_today(now))
        exp_rem = sum(sc.SmartCooling._clearsky_wm(h + 0.5, 5.0, 21.0, 700.0)
                      for h in range(13, 24))
        self.assertAlmostEqual(sm, (100.0 * 12.0 + exp_rem) / 24.0, places=4)

    def test_cloud_forecast_attenuates_remaining_term(self):
        # full overcast (cloud=1.0) over every remaining hour -> each clear-sky hour times
        # (1 - 0.75*1.0) = 0.25
        fc = [{"dt": datetime(2026, 7, 20, h), "temp": 20.0, "cloud": 1.0}
              for h in range(13, 24)]
        app = self._app(measured_mean=100.0, forecast=fc)
        now = datetime(2026, 7, 20, 12, 0)
        sm = self._run(app._solar_mean_today(now))
        exp_rem = sum(sc.SmartCooling._clearsky_wm(h + 0.5, 5.0, 21.0, 700.0) * 0.25
                      for h in range(13, 24))
        self.assertAlmostEqual(sm, (100.0 * 12.0 + exp_rem) / 24.0, places=4)

    def test_missing_solar_sensor_returns_none(self):
        app = self._app(measured_mean=100.0, solar_state="unavailable")
        sm = self._run(app._solar_mean_today(datetime(2026, 7, 20, 12, 0)))
        self.assertIsNone(sm)

    def test_failed_history_returns_none(self):
        app = self._app(measured_mean=None)   # history mean unavailable
        sm = self._run(app._solar_mean_today(datetime(2026, 7, 20, 12, 0)))
        self.assertIsNone(sm)

    def test_evening_is_nearly_fully_measured(self):
        # after sunset there is no remaining daylight, so solar_mean = measured*elapsed/24
        app = self._app(measured_mean=250.0)
        now = datetime(2026, 7, 20, 22, 0)   # elapsed 22h, no daylight hours left
        sm = self._run(app._solar_mean_today(now))
        self.assertAlmostEqual(sm, 250.0 * 22.0 / 24.0, places=4)


class WeatherEquilibrium(unittest.TestCase):
    """Model D equilibrium + its fallbacks and the one-sided safety floor. The method always
    computes (shadow gating lives in _evaluate_locked); every missing input / disabled /
    unseeded / exception path returns EXACTLY the legacy proxy."""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _app(self, solar_mean=200.0, outdoor_max=31.0, prev=27.0, forecast=object(),
             **ov):
        app = make_app(prev_kitchen_max=prev, **ov)
        async def _sm(now):
            return solar_mean
        app._solar_mean_today = _sm
        async def _om(now):
            return outdoor_max
        app._outdoor_max_today = _om
        async def _fc(now):
            return forecast
        app._get_forecast = _fc
        return app

    def test_disabled_returns_legacy(self):
        app = self._app(weather_model_enabled=False)
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=22.5))
        self.assertEqual(E, 22.5)
        self.assertIsNone(dbg["equilibrium_weather"])
        self.assertEqual(dbg["equilibrium_legacy"], 22.5)

    def test_unseeded_prev_returns_legacy(self):
        app = self._app(prev=None)
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=22.5))
        self.assertEqual(E, 22.5)
        self.assertIsNone(dbg["equilibrium_weather"])

    def test_missing_solar_returns_legacy(self):
        app = self._app(solar_mean=None)
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=22.5))
        self.assertEqual(E, 22.5)
        self.assertIsNone(dbg["equilibrium_weather"])

    def test_hot_day_guard_no_forecast_before_peak_hour_returns_legacy(self):
        app = self._app(forecast=None)   # forecast fetch failed
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=22.5))
        self.assertEqual(E, 22.5)   # 09:00 < wm_peak_hour(15) -> legacy
        self.assertIsNone(dbg["equilibrium_weather"])

    def test_no_forecast_after_peak_hour_still_computes(self):
        # after 15:00 a missing forecast is tolerable (peak measured); model proceeds
        app = self._app(forecast=None, solar_mean=12.0, outdoor_max=16.0, prev=22.7)
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 18, 0), 22.0, 22.0, 22.0, e_legacy=23.2))
        self.assertIsNotNone(dbg["equilibrium_weather"])

    def test_active_formula_hot_day_deep_early(self):
        # design worked example: hot day at 09:00, solar_mean~200, outdoor_max=31, prev=27
        # E_apartment = 15.797 + 0.0162*200 + 0.198*(31-24) + 0.287*27 = 28.174
        # E_weather = +0.5 = 28.674; e_legacy=22.5 -> max(28.67, 21.0) = 28.67
        app = self._app(solar_mean=200.0, outdoor_max=31.0, prev=27.0)
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=22.5))
        self.assertAlmostEqual(dbg["kitchen_max_pred"], 28.17, places=1)
        self.assertAlmostEqual(dbg["equilibrium_weather"], 28.67, places=1)
        self.assertAlmostEqual(E, 28.67, places=1)   # model wins the max()

    def test_relief_floor_caps_how_far_below_legacy(self):
        # cool sunless day but a warm resting kitchen reading: model predicts well below
        # legacy, but E can't drop more than wm_nowcast_relief(1.5) under it.
        app = self._app(solar_mean=12.0, outdoor_max=16.0, prev=22.7)
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 18, 0), 26.0, 26.0, 26.0, e_legacy=26.5))
        # E_weather = 15.797 + 0.0162*12 + 0 + 0.287*22.7 + 0.5 = 23.005
        self.assertAlmostEqual(dbg["equilibrium_weather"], 23.0, places=1)
        self.assertAlmostEqual(E, 25.0, places=2)   # floored at 26.5 - 1.5

    def test_exception_falls_back_to_legacy(self):
        app = self._app()
        async def _boom(now):
            raise RuntimeError("forecast blew up")
        app._solar_mean_today = _boom
        E, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=22.5))
        self.assertEqual(E, 22.5)
        self.assertIsNone(dbg["equilibrium_weather"])

    def test_shadow_selection_keeps_legacy_driving(self):
        # the method itself always computes a real weather E; the shadow gate (in
        # _evaluate_locked) is what keeps legacy driving. Verify both halves here.
        app = self._app(solar_mean=200.0, outdoor_max=31.0, prev=27.0, wm_shadow=True)
        e_legacy = 22.5
        e_active, dbg = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=e_legacy))
        self.assertGreater(e_active, e_legacy)                 # model computed a hotter E
        self.assertIsNotNone(dbg["equilibrium_weather"])       # published for comparison
        # the exact expression _evaluate_locked uses:
        E = e_active if (app.weather_model_enabled and not app.wm_shadow) else e_legacy
        self.assertEqual(E, e_legacy)                          # shadow -> legacy still drives

    def test_not_shadow_selection_drives_weather(self):
        app = self._app(solar_mean=200.0, outdoor_max=31.0, prev=27.0, wm_shadow=False)
        e_legacy = 22.5
        e_active, _ = self._run(app._weather_equilibrium(
            datetime(2026, 7, 20, 9, 0), 22.0, 22.0, 22.0, e_legacy=e_legacy))
        E = e_active if (app.weather_model_enabled and not app.wm_shadow) else e_legacy
        self.assertEqual(E, e_active)
        self.assertGreater(E, e_legacy)


class TrackKitchenMax(unittest.TestCase):
    """Running daily max of the kitchen temperature with a local-midnight rollover:
    yesterday's peak becomes prev_kitchen_max (Model D's thermal-mass memory) and today's
    max resets on the first tick of a new calendar day."""

    def test_running_max_same_day(self):
        app = make_app(kitchen_max_today=None, kitchen_max_date=None)
        app._track_kitchen_max(datetime(2026, 7, 20, 10, 0), 24.0)
        self.assertEqual(app._kitchen_max_today, 24.0)
        app._track_kitchen_max(datetime(2026, 7, 20, 14, 0), 26.5)
        self.assertEqual(app._kitchen_max_today, 26.5)
        app._track_kitchen_max(datetime(2026, 7, 20, 16, 0), 25.0)   # not a new high
        self.assertEqual(app._kitchen_max_today, 26.5)

    def test_midnight_rollover_moves_today_to_prev(self):
        app = make_app(kitchen_max_today=27.3, kitchen_max_date="2026-07-20",
                       prev_kitchen_max=None)
        app._track_kitchen_max(datetime(2026, 7, 21, 0, 15), 22.0)
        self.assertEqual(app._prev_kitchen_max, 27.3)
        self.assertEqual(app._kitchen_max_today, 22.0)
        self.assertEqual(app._kitchen_max_date, "2026-07-21")

    def test_multiday_gap_drops_stale_memory_instead_of_promoting(self):
        # Stored peak is two days old (e.g. downtime): must NOT become prev_kitchen_max.
        app = make_app(kitchen_max_today=27.3, kitchen_max_date="2026-07-18",
                       prev_kitchen_max=25.0)
        app._track_kitchen_max(datetime(2026, 7, 20, 0, 15), 22.0)
        self.assertIsNone(app._prev_kitchen_max)   # stale memory dropped -> _seed rebuilds
        self.assertEqual(app._kitchen_max_today, 22.0)
        self.assertEqual(app._kitchen_max_date, "2026-07-20")

    def test_none_reading_does_not_lower_running_max(self):
        app = make_app(kitchen_max_today=26.0, kitchen_max_date="2026-07-20")
        app._track_kitchen_max(datetime(2026, 7, 20, 15, 0), None)
        self.assertEqual(app._kitchen_max_today, 26.0)

    def test_first_ever_tick_seeds_today_no_prev(self):
        app = make_app(kitchen_max_today=None, kitchen_max_date=None, prev_kitchen_max=None)
        app._track_kitchen_max(datetime(2026, 7, 20, 9, 0), 23.4)
        self.assertIsNone(app._prev_kitchen_max)   # nothing to roll over yet
        self.assertEqual(app._kitchen_max_today, 23.4)
        self.assertEqual(app._kitchen_max_date, "2026-07-20")


class EveningRescue(unittest.TestCase):
    """The evening rescue DELIVERS the sleep plan's verdict -- one brain (2026-07-23: it
    used to recompute its own projection from the un-grounded kitchen proxy and pushed
    "deploy the AC" at a 21.6C bedroom while the plan said windows). Fires only when the
    current plan says "ac" and every gate holds; never a climate command."""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _app(self, plan_rec="ac", equilibrium=25.0, limit=23.0, peak=24.5, home="home",
             rescue_enabled=True, from_hour=16, to_hour=23, deficit_min=0.5,
             notified_date=None, has_plan=True):
        app = make_app(weather_model_enabled=True, wm_shadow=True)
        app.rescue_enabled = rescue_enabled
        app.rescue_from_hour = from_hour
        app.rescue_to_hour = to_hour
        app.rescue_deficit_min = deficit_min
        app.rescue_home_entity = "person.mikkel"
        app.floor_cool_cph = 1.0
        app.zone_offset = 1.0
        app.min_temp = 16.0
        app._rescue_notified_date = notified_date
        app._last_plan = ({"rec": plan_rec, "equilibrium": equilibrium,
                           "limit": limit, "peak": peak} if has_plan else None)

        async def _state(entity):
            return home
        app._state = _state

        app._notified = []

        async def _notify(msg):
            app._notified.append(msg)
        app._notify = _notify
        return app

    def _fire(self, app, now, floor):
        self._run(app._maybe_evening_rescue(now, floor))

    def test_fires_once_when_all_conditions_hold(self):
        # floor 22.5, limit 23 -> cap 22; E 25, r=0.5 -> target 19; deficit 3.5 >= 0.5;
        # 210 min needed, 19:00 leaves 240 -> feasible; plan says ac; home -> fires.
        app = self._app()
        now = datetime(2026, 7, 20, 19, 0)
        self._fire(app, now, floor=22.5)
        self.assertEqual(len(app._notified), 1)
        self.assertIn("arm Cool night", app._notified[0])
        self.assertEqual(app._rescue_notified_date, "2026-07-20")
        # second call the same evening is deduped
        self._fire(app, now, floor=22.5)
        self.assertEqual(len(app._notified), 1)

    def test_aug_2026_silence_regression_deep_target_no_longer_blocks(self):
        # The Aug 2-5 2026 silence: E 26 with a trusted feasible floor 20.33 gave an
        # uncapped target ~19, and the crawl regime (0.4C/h under the wall) priced the
        # deficit at ~6h -- more evening than exists, so the gate never passed while four
        # straight nights broke the ceiling. Capped target + wall-gated minutes must fire.
        app = self._app(equilibrium=26.0, limit=23.0)
        app._feasible_floor = 20.33
        app._feasible_samples = 5
        app.feasible_min_samples = 2
        app.feasible_probe_c = 1.0
        app._cool_cph = 1.2
        app.cool_cph_min, app.cool_cph_max = 0.3, 4.0
        app.rate_learn_min_headroom = 0.7
        app.crawl_rate_cph = 0.4
        now = datetime(2026, 8, 4, 18, 30)
        self._fire(app, now, floor=23.8)
        self.assertEqual(len(app._notified), 1)

    def test_still_blocks_when_even_the_wall_cannot_be_reached_in_time(self):
        # 22:40: twenty minutes of window left cannot pre-cool a 3.5C deficit - stay silent.
        app = self._app(equilibrium=26.0, limit=23.0)
        app._feasible_floor = 20.33
        app._feasible_samples = 5
        app.feasible_min_samples = 2
        app.feasible_probe_c = 1.0
        app._cool_cph = 1.2
        app.cool_cph_min, app.cool_cph_max = 0.3, 4.0
        app.rate_learn_min_headroom = 0.7
        app.crawl_rate_cph = 0.4
        self._fire(app, datetime(2026, 8, 4, 22, 40), floor=23.8)
        self.assertEqual(app._notified, [])

    def test_windows_plan_never_fires(self):
        # THE 2026-07-23 regression: a warm-looking equilibrium but the plan's verdict is
        # windows (bedroom 21.6C, cool night incoming) -> the rescue must stay silent.
        app = self._app(plan_rec="windows", equilibrium=25.0)
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._rescue_notified_date)

    def test_hybrid_and_nothing_plans_never_fire(self):
        for rec in ("hybrid", "nothing"):
            app = self._app(plan_rec=rec)
            self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
            self.assertEqual(app._notified, [], f"rec={rec} must not push")

    def test_no_plan_yet_never_fires(self):
        app = self._app(has_plan=False)
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
        self.assertEqual(app._notified, [])

    def test_disabled_never_fires(self):
        app = self._app(rescue_enabled=False)
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
        self.assertEqual(app._notified, [])

    def test_outside_hours_suppressed(self):
        app = self._app()
        self._fire(app, datetime(2026, 7, 20, 9, 0), floor=22.5)   # before from_hour
        self.assertEqual(app._notified, [])
        app2 = self._app()
        self._fire(app2, datetime(2026, 7, 20, 23, 0), floor=22.5)  # == to_hour (exclusive)
        self.assertEqual(app2._notified, [])

    def test_deficit_below_min_suppressed(self):
        # floor 19.1 vs target 19 -> deficit 0.1 < 0.5
        app = self._app()
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=19.1)
        self.assertEqual(app._notified, [])

    def test_not_feasible_suppressed(self):
        # deep deficit but too late: floor 22.5, target 19 -> 210 min needed, but at 22:00
        # only (23-22)*60 = 60 min remain before the cutoff.
        app = self._app()
        self._fire(app, datetime(2026, 7, 20, 22, 0), floor=22.5)
        self.assertEqual(app._notified, [])

    def test_away_suppresses_without_consuming_the_day(self):
        # user 2026-07-23 (firm): a push while away is pure stress -- nothing can be done
        # from there. Suppress on a live not_home, but do NOT mark the day sent: coming
        # home while the night is still saveable delivers it on the next tick.
        app = self._app(home="not_home")
        # floor 20.0 -> target 19 -> deficit 1.0 (60 min): rescuable all evening.
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=20.0)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._rescue_notified_date)
        # ...they come home at 21:00 with 120 min left for a 60-min deficit -> fires now.
        async def _state_home(entity):
            return "home"
        app._state = _state_home
        self._fire(app, datetime(2026, 7, 20, 21, 0), floor=20.0)
        self.assertEqual(len(app._notified), 1)
        self.assertIn("arm Cool night", app._notified[0])

    def test_home_sensor_missing_does_not_suppress(self):
        for missing in (None, "unknown", "unavailable"):
            app = self._app(home=missing)
            self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
            self.assertEqual(len(app._notified), 1, f"missing={missing!r} should still fire")

    def test_already_notified_today_suppressed(self):
        app = self._app(notified_date="2026-07-20")
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
        self.assertEqual(app._notified, [])

    def test_message_quotes_the_plans_numbers(self):
        app = self._app(peak=24.5, limit=23.0)
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
        self.assertEqual(len(app._notified), 1)
        self.assertIn("24.5°", app._notified[0])
        self.assertIn("23.0° limit", app._notified[0])

    def test_peakless_plan_still_reads_clean(self):
        app = self._app(peak=None)
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)
        self.assertEqual(len(app._notified), 1)
        self.assertNotIn("None", app._notified[0])
        self.assertIn("vs the 23.0° limit", app._notified[0])

    def test_error_is_swallowed_no_raise(self):
        app = self._app()

        def _boom(E, ceiling):
            raise RuntimeError("boom")
        app._calc_target = _boom
        self._fire(app, datetime(2026, 7, 20, 19, 0), floor=22.5)   # must not raise
        self.assertEqual(app._notified, [])


class EvaluateTickWiring(unittest.TestCase):
    """Change 1 + Change 2 wiring through _evaluate_locked: read + publish EVERY tick
    regardless of arm/deploy state, and call the evening-rescue helper only from the
    disarmed branch. Actuation methods are stubbed -- these assert the read-only plumbing,
    not the (unchanged) armed decision chain."""

    WM_DBG = {
        "equilibrium_weather": 28.6, "equilibrium_legacy": 24.5,
        "solar_mean_est": 210.0, "outdoor_max_est": 31.0,
        "kitchen_max_pred": 28.1, "prev_kitchen_max": 27.0,
    }

    def _app(self, enable="off", climate="off", master_was_on=False,
             kitchen=26.0, mid=25.0, floor=25.0):
        app = make_app(weather_model_enabled=True, wm_shadow=True)
        app.enable_entity = "en"
        app.climate_entity = "cl"
        app.floor_sensor = "floor"
        app.mid_sensor = "mid"
        app.kitchen_sensor = "kitchen"
        app.bathroom_sensor = "bath"
        app.outdoor_sensor = "out"
        app.bathroom_delta_max = 12.0
        app.person_offset = 0.5
        # read every tick by the session-cost metering wiring (see _track_session_cost);
        # neither is in `nums` below, so _num() returns its own None default for both --
        # a real Shelly/price outage, harmlessly a no-op for _track_session_cost.
        app.price_entity = "price"
        app.ac_energy_entity = "ac_energy"
        app._master_was_on = master_was_on
        app._not_deployed_since = None
        app._deploy_watchdog_notified = False
        app._safety_off_notified = False
        app._kitchen_max_today = None
        app._kitchen_max_date = None
        app._prev_kitchen_max = None
        app._last_eval_at = None
        app._last_want = False

        nums = {"kitchen": kitchen, "mid": mid, "floor": floor}
        states = {"en": enable, "cl": climate}

        async def get_now():
            return datetime(2026, 7, 20, 19, 0)
        app.get_now = get_now

        async def _num(entity, default):
            return nums.get(entity, default)   # bath/out -> None (no hazard)
        app._num = _num

        async def _state(entity):
            return states.get(entity)
        app._state = _state

        async def _learn(now):
            return
        app._learn = _learn

        async def _we(now, k, m, f, el):
            return el, dict(self.WM_DBG)   # e_active == e_legacy (shadow); dbg published
        app._weather_equilibrium = _we

        async def _cdw(now, master_on, deployed):
            return
        app._check_deploy_watchdog = _cdw

        app._rescue_calls = []

        async def _rescue(now, floor):
            app._rescue_calls.append(floor)
        app._maybe_evening_rescue = _rescue

        # sensor.sleep_plan publisher is called every tick BEFORE the arm gate; stub it so
        # the wiring test asserts it's invoked (and never issues a climate command) without
        # exercising its I/O. _effective_ceiling is also stubbed (armed branch uses it).
        app._sleep_plan_calls = []

        async def _psp(now, floor, e_active):
            app._sleep_plan_calls.append((floor, e_active))
        app._publish_sleep_plan = _psp

        async def _eff(now):
            return 23.0, 23.0
        app._effective_ceiling = _eff

        app._published = []

        async def _publish(status, reason, attrs):
            app._published.append((status, reason, dict(attrs)))
        app._publish = _publish

        async def _ensure_off(status, reason, attrs):
            app._published.append((status, reason, dict(attrs)))
        app._ensure_off = _ensure_off
        return app

    def _run(self, app):
        import asyncio
        asyncio.run(app._evaluate_locked())

    def test_disarmed_publish_includes_weather_attrs(self):
        app = self._app(enable="off", climate="off")
        self._run(app)
        self.assertEqual(len(app._published), 1)
        status, _, attrs = app._published[0]
        self.assertEqual(status, "off")
        # Change 1: wm_dbg merged into the disarmed publish, existing key preserved.
        self.assertEqual(attrs["deployed"], True)
        self.assertIn("equilibrium_weather", attrs)
        self.assertEqual(attrs["equilibrium_legacy"], 24.5)

    def test_track_kitchen_max_runs_on_disarmed_tick(self):
        app = self._app(enable="off", climate="off", kitchen=26.0)
        self.assertIsNone(app._kitchen_max_today)
        self._run(app)
        # Change 1: measurement now happens even while disarmed.
        self.assertEqual(app._kitchen_max_today, 26.0)
        self.assertEqual(app._kitchen_max_date, "2026-07-20")

    def test_rescue_helper_invoked_from_disarmed_branch(self):
        app = self._app(enable="off", climate="off")
        self._run(app)
        self.assertEqual(len(app._rescue_calls), 1)

    def test_sleep_plan_published_on_disarmed_tick(self):
        # advisory sleep plan runs BEFORE the arm gate -> every branch, with e_active.
        app = self._app(enable="off", climate="off", floor=25.0)
        self._run(app)
        self.assertEqual(len(app._sleep_plan_calls), 1)
        floor_arg, e_active_arg = app._sleep_plan_calls[0]
        self.assertEqual(floor_arg, 25.0)

    def test_armed_but_not_deployed_publishes_weather_and_skips_rescue(self):
        # master on, climate unavailable -> not-deployed branch: still publishes wm_dbg,
        # never reaches (nor calls) the disarmed-only rescue helper, but the sleep plan
        # (before the arm gate) still runs.
        app = self._app(enable="on", climate="unavailable")
        self._run(app)
        self.assertEqual(len(app._rescue_calls), 0)
        self.assertEqual(len(app._sleep_plan_calls), 1)
        status, _, attrs = app._published[0]
        self.assertEqual(status, "unit_stored")
        self.assertIn("equilibrium_weather", attrs)

    def test_disarmed_undeployed_finalizes_a_dangling_session(self):
        # Fallback session close-out (see SessionCostTracking): the user unplugged the AC
        # without pressing "AC removed" first, so the normal _finalize_session() call in
        # that press's branch never ran. Disarmed + not-deployed is exactly the resting
        # state a genuinely-ended night settles into, so that tick must close it out
        # instead of metering a dangling session forever.
        app = self._app(enable="off", climate="unavailable")
        app._session_kwh0 = 10.0
        app._session_last_counter = 10.0
        app._session_kwh = 1.2
        app._session_cost = 1.5
        self._run(app)
        self.assertIsNone(app._session_kwh0)
        self.assertIsNone(app._session_last_counter)
        self.assertEqual(app._last_session_cost, 1.5)
        self.assertEqual(app._night_cost_samples, 1)

    def test_deployed_and_disarmed_does_not_finalize_a_session(self):
        # deployed (climate reachable, even if "off") -> not the fallback's trigger state;
        # a live session must be left open for the normal AC-removed-press close-out.
        app = self._app(enable="off", climate="off")
        app._session_kwh0 = 10.0
        app._session_last_counter = 10.0
        app._session_kwh = 1.2
        app._session_cost = 1.5
        self._run(app)
        self.assertEqual(app._session_kwh0, 10.0)
        self.assertIsNone(app._last_session_cost)


class PublishSleepPlanGrounding(unittest.TestCase):
    """End-to-end _publish_sleep_plan for the 2026-07-22 drift: the weather model's DAYTIME
    peak (e_active ~24.7) fed straight into the plan recommended cooling a flat that's already
    ~21.7C on a cool (~15C) night. Grounding caps the projected equilibrium at bedroom_zone_now
    + margin, so the plan flips OFF the AC. Exercises the real grounding math + bedroom-zone anchor
    + _night_outdoor_min; only the leaf I/O (_num/_state/_attr/_get_forecast/_effective_ceiling/
    set_state) is stubbed. ADVISORY ONLY -- asserts NO climate command is ever issued."""

    NOW = datetime(2026, 7, 22, 12, 0)

    def _app(self, e_active=24.7, floor=22.0, warm_night_margin=1.0):
        app = make_app(rise_frac=0.5)
        app.comfort_temp_entity = "sensor.bed_temp"
        app.comfort_rh_entity = "sensor.bed_rh"
        app.outdoor_sensor = "sensor.outdoor"
        app.outdoor_rh_entity = "sensor.outdoor_rh"
        app.mid_sensor = "sensor.mid"
        app.kitchen_sensor = "sensor.kitchen"
        app.floor_sensor = "sensor.floor"
        app.price_entity = "sensor.price"
        app.weather_forecast_entity = "weather.forecast_home"
        app.sleep_plan_entity = "sensor.sleep_plan"
        app.window_contact_entities = {"bedroom": "binary_sensor.bedroom_window"}
        app.climate_entity = "climate.ac"
        app.enable_entity = "input_boolean.smart_cooling"
        app.zone_offset = 1.0
        app.floor_cool_cph = 1.0
        app.cool_kw = 0.5
        app.ac_noise_penalty_kr = 0.5
        app.wm_reality_margin = 1.0
        app.vent_tau_h = 7.0
        app.wm_warm_night_margin = warm_night_margin

        # Bedroom zone ~21.7-22.0C now (floor 22.0 arg, mid 21.7, kitchen 21.9); outdoor 15C.
        nums = {"sensor.bed_temp": 21.7, "sensor.bed_rh": 60.0,
                "sensor.outdoor": 15.0, "sensor.outdoor_rh": 60.0,
                "sensor.mid": 21.7, "sensor.kitchen": 21.9,
                "sensor.price": 1.7}

        async def _num(entity, default):
            return nums.get(entity, default)
        app._num = _num

        async def _state(entity):
            return "on" if entity == "binary_sensor.bedroom_window" else None
        app._state = _state

        async def _attr(entity, key, default=None):
            return default   # no price arrays -> empty price map, cheapest = price_now
        app._attr = _attr

        async def _fc(now):
            return [{"dt": datetime(2026, 7, 22, 20, 0), "temp": 16.0, "cloud": None},
                    {"dt": datetime(2026, 7, 23, 3, 0), "temp": 15.0, "cloud": None},
                    {"dt": datetime(2026, 7, 23, 6, 0), "temp": 15.5, "cloud": None}]
        app._get_forecast = _fc

        async def _eff(now):
            return 22.5, 23.0   # comfort-lowered ceiling tonight
        app._effective_ceiling = _eff

        app._set_state_calls = []

        async def set_state(entity, **kw):
            app._set_state_calls.append((entity, kw))
        app.set_state = set_state
        app._e_active = e_active
        app._floor = floor
        return app

    def _run(self, app):
        import asyncio
        asyncio.run(app._publish_sleep_plan(self.NOW, app._floor, app._e_active))
        self.assertEqual(len(app._set_state_calls), 1)
        entity, kw = app._set_state_calls[0]
        self.assertEqual(entity, "sensor.sleep_plan")
        return kw["state"], kw["attributes"]

    def test_cool_apartment_cool_night_flips_off_ac(self):
        app = self._app()
        state, attrs = self._run(app)
        # THE fix: grounded -> no AC. Not 'ac', not 'hybrid' (both involve the compressor).
        self.assertIn(state, ("nothing", "windows"))
        self.assertNotIn(state, ("ac", "hybrid"))
        self.assertEqual(attrs["recommendation"], state)
        # transparency attrs: raw weather peak vs the grounded value actually projected
        self.assertEqual(attrs["grounded"], "true")
        self.assertEqual(attrs["equilibrium_weather"], 24.7)
        # The anchor is the zone AT BEDTIME, not the reading at plan time: this harness has
        # windows open and cool air outside, so the zone vents down before the room is sealed
        # (2026-08-01). equilibrium_planned therefore sits below the old zone_now + 1.0 = 23.0.
        self.assertLess(attrs["equilibrium_planned"], 23.0)
        self.assertEqual(attrs["bedroom_zone_now"], 22.0)      # max of floor/mid/kitchen
        self.assertLess(attrs["zone_at_bedtime"], 22.0)        # projected forward, published
        self.assertEqual(attrs["night_outdoor_min"], 15.0)
        self.assertIn("Grounded on reality", attrs["detail"])

    def test_grounding_is_what_flips_it_warm_night_still_wants_ac(self):
        # Same cool flat, but force the "warm night" branch (huge warm_night_margin) so the
        # raw daytime peak 24.7 drives the plan -> the AC is back in the recommendation.
        app = self._app(warm_night_margin=100.0)
        state, attrs = self._run(app)
        self.assertEqual(attrs["grounded"], "false")
        self.assertIn(state, ("ac", "hybrid"))          # AC involved without grounding
        self.assertEqual(attrs["equilibrium_planned"], 24.7)   # ungrounded = raw weather peak

    def test_advisory_only_issues_no_climate_command(self):
        # The publisher must touch ONLY set_state(sensor.sleep_plan); assert it never calls
        # call_service (a climate command would be a hard bug -- this sensor is advisory).
        app = self._app()
        app._called_services = []

        async def call_service(*a, **k):
            app._called_services.append((a, k))
        app.call_service = call_service
        self._run(app)
        self.assertEqual(app._called_services, [])

    def test_bedtime_at_is_wake_minus_sleep_window(self):
        # The dashboard's bedtime boundary must be THIS published number, never a card-side
        # constant (2026-07-30: the bar's hardcoded 23:00 and the schedule list drifted apart
        # in the user's reading). Harness NOW is a Wednesday noon -> fallback wake 07:00.
        app = self._app()
        _, attrs = self._run(app)
        self.assertEqual(attrs["wake_at"], "07:00")
        self.assertEqual(attrs["bedtime_at"], "23:00")

    def test_missing_zone_sensors_fall_back_to_raw_weather(self):
        # If every bedroom-zone reading is None AND floor is None, bedroom_zone_now is None ->
        # grounding can't anchor -> raw e_active is used (None-safe, errs warm).
        app = self._app(floor=None)

        async def _num_none(entity, default):
            return default   # all temp sensors missing (default None); price keeps its 1.7
        app._num = _num_none
        state, attrs = self._run(app)
        self.assertEqual(attrs["grounded"], "false")
        self.assertNotIn("bedroom_zone_now", attrs)   # None-valued attrs are omitted


class PublishSleepPlanPricing(unittest.TestCase):
    """2026-07-29: pricing must blend over the slots the job will ACTUALLY occupy
    (k = ceil(minutes_needed / 15), minutes_needed = deficit / floor_cool_cph * 60), not a
    fixed est_price_slots or the single cheapest slot -- real blended price paid across 15
    metered days (2026-07-10..29) was 1.39-1.52 kr/kWh vs the ~0.5 kr/kWh the old
    single-cheapest-slot code produced. est_price_slots remains the fallback k for when the
    deficit can't be computed (missing floor or equilibrium)."""

    NOW = datetime(2026, 7, 29, 12, 0)

    def _app(self, floor):
        app = make_app(rise_frac=0.5)
        app.comfort_temp_entity = "t"
        app.comfort_rh_entity = "rh"
        app.outdoor_sensor = "out"
        app.outdoor_rh_entity = "out_rh"
        app.mid_sensor = "mid"
        app.kitchen_sensor = "kitchen"
        app.floor_sensor = "floor"
        app.price_entity = "price"
        app.weather_forecast_entity = "wx"
        app.sleep_plan_entity = "sensor.sleep_plan"
        app.window_contact_entities = {}
        app.zone_offset = 1.0
        app.floor_cool_cph = 1.0
        app.cool_kw = 0.5
        app.ac_noise_penalty_kr = 0.5
        app.wm_reality_margin = 1.0
        app.vent_tau_h = 7.0
        # Huge margin -> grounded_equilibrium's "warm night" branch always wins, so
        # plan_equilibrium == e_active (26.0) exactly and the target/deficit math below is
        # easy to hand-check (equally true via the apartment_now-None fallback when floor
        # is None -- either way plan_equilibrium stays the raw e_active).
        app.wm_warm_night_margin = 100.0
        app.est_price_slots = 8

        nums = {"t": 22.0, "rh": 50.0, "out": 15.0, "out_rh": 50.0,
               "mid": floor, "kitchen": floor, "price": 1.5}

        async def _num(entity, default):
            return nums.get(entity, default)
        app._num = _num

        async def _state(entity):
            return None
        app._state = _state

        async def _attr(entity, key, default=None):
            return default   # no price arrays -> empty price map, cheapest = price_now
        app._attr = _attr

        async def _fc(now):
            return []
        app._get_forecast = _fc

        async def _eff(now):
            return 22.5, 22.5   # ceiling, ceiling_base
        app._effective_ceiling = _eff

        app._set_state_calls = []

        async def set_state(entity, **kw):
            app._set_state_calls.append((entity, kw))
        app.set_state = set_state

        # Spy on the real _blended_cheap so k is captured without faking the pricing math.
        real_blended_cheap = app._blended_cheap
        app._blended_calls = []

        def _blended_cheap_spy(pm, now, deadline, fallback, k):
            app._blended_calls.append(k)
            return real_blended_cheap(pm, now, deadline, fallback, k)
        app._blended_cheap = _blended_cheap_spy

        app._floor = floor
        app._e_active = 26.0
        return app

    def _run(self, app):
        import asyncio
        asyncio.run(app._publish_sleep_plan(self.NOW, app._floor, app._e_active))

    def test_k_sized_from_the_deficit_not_the_fixed_fallback(self):
        # target 17.0 (calc_floor_target: cap 21.5, r 0.5, E 26.0), deficit 24.0-17.0=7.0C
        # -> 420 min -> ceil(420/15) = 28 slots, not the fixed est_price_slots (8).
        app = self._app(floor=24.0)
        self._run(app)
        self.assertEqual(app._blended_calls, [28])

    def test_small_deficit_still_prices_at_least_one_slot(self):
        # deficit 0.1C -> 6 min -> ceil(6/15) = 1, floored at 1 (never 0 slots).
        app = self._app(floor=17.1)
        self._run(app)
        self.assertEqual(app._blended_calls, [1])

    def test_unknown_deficit_falls_back_to_est_price_slots(self):
        # floor missing -> the deficit can't be computed -> est_price_slots is the k floor.
        app = self._app(floor=None)
        self._run(app)
        self.assertEqual(app._blended_calls, [8])


class GoldenModelMath(unittest.TestCase):
    """Lock the byte-identical-actuation claim into CI: the shared climate_model fns
    reproduce smart_cooling's FORMER inline math on a fixed grid. The `_old_*` bodies below
    are verbatim copies of the pre-refactor inline code; every grid point must match cm.*."""

    def _old_equilibrium(self, kitchen, mid, floor, person_offset):
        vals = [v for v in (kitchen, mid, floor) if v is not None]
        return (max(vals) if vals else 24.5) + person_offset

    def _old_calc_target(self, E, ceiling, rise_frac, zone_offset, min_temp):
        cap = ceiling - zone_offset
        r = min(0.95, max(0.05, rise_frac))
        if E <= cap:
            return ceiling
        f0 = (cap - E * r) / (1.0 - r)
        return max(min_temp, min(ceiling, round(f0, 2)))

    def _old_apartment(self, solar_mean, outdoor_max, prev, b0, bs, bv, knee, bp):
        return b0 + bs * solar_mean + bv * max(0.0, outdoor_max - knee) + bp * prev

    def test_legacy_equilibrium_grid(self):
        for k in (None, 20.0, 24.3):
            for m in (None, 21.5):
                for f in (None, 19.0, 25.1):
                    for po in (0.0, 0.5, 1.0):
                        self.assertEqual(cm.legacy_equilibrium(k, m, f, po),
                                         self._old_equilibrium(k, m, f, po))

    def test_calc_floor_target_grid(self):
        for E in (18.0, 21.0, 22.0, 22.05, 25.0, 25.333, 30.0, 40.0):
            for ceiling in (21.0, 23.0):
                for rise in (0.0, 0.05, 0.3, 0.5, 0.7, 0.95, 0.99):
                    for zone in (0.5, 1.0):
                        self.assertEqual(
                            cm.calc_floor_target(E, ceiling, rise, zone, 16.0),
                            self._old_calc_target(E, ceiling, rise, zone, 16.0),
                            msg=f"E={E} ceil={ceiling} rise={rise} zone={zone}")

    def test_model_d_apartment_grid(self):
        coeffs = cm.ModelDCoeffs(15.797, 0.0162, 0.198, 24.0, 0.287)
        for sm in (0.0, 50.0, 200.0, 400.0):
            for om in (16.0, 24.0, 31.0):
                for prev in (20.0, 27.0):
                    self.assertEqual(
                        cm.model_d_apartment(sm, om, prev, coeffs),
                        self._old_apartment(sm, om, prev, 15.797, 0.0162, 0.198, 24.0, 0.287))


class EffectiveCeilingEquivalence(unittest.TestCase):
    """The intentional cycle-break is on the INPUT side: smart_cooling._effective_ceiling now
    computes the ceiling locally via climate_model instead of reading sensor.bedroom_comfort.
    Prove it is numerically the SAME value bedroom_comfort publishes (same fn, same entities,
    same params), so the driven target stays byte-identical."""

    def _app(self, t_in, rh_in, ceiling_base=23.0, persons_home=("person.mikkel",)):
        app = make_app()
        app.night_ceiling_entity = "input_number.nc"
        app.default_ceiling = 23.0
        app.comfort_temp_entity = "sensor.bedroom_median_temperature"
        app.comfort_rh_entity = "sensor.bedroom_humidity"
        app.comfort_persons = ["person.mikkel"]
        app.comfort_anchor = 23.0
        app.comfort_dp_rate = 0.5
        app.comfort_knee = 12.0
        app.comfort_penalty = 0.15
        app.comfort_second_sleeper = 0.5
        app.comfort_max_reduction = 1.5
        app.min_temp = 16.0
        nums = {"input_number.nc": ceiling_base,
                "sensor.bedroom_median_temperature": t_in,
                "sensor.bedroom_humidity": rh_in}

        async def _num(entity, default):
            return nums.get(entity, default)
        app._num = _num

        async def _state(entity):
            return "home" if entity in persons_home else "not_home"
        app._state = _state
        return app

    def _bedroom_comfort_ceiling(self, t_in, rh_in, now, sleepers=1,
                                 anchor=23.0, base=23.0, max_red=1.5):
        # exactly what bedroom_comfort computes for ceiling_effective, then the smart_cooling
        # clamp against the knob.
        dp = cm.dew_point_c(t_in, rh_in)
        dp_m = cm.project_morning_dp(dp, sleepers, cm.hours_until_morning(now), 0.5)
        ce, _ = cm.effective_ceiling(anchor, dp_m, sleepers, 12.0, 0.15, 0.5, max_red)
        return min(base, max(ce, base - max_red, 16.0))

    def test_humid_single_sleeper_matches_comfort_layer(self):
        import asyncio
        t_in, rh_in = 24.1, 60.0
        now = datetime(2026, 7, 20, 23, 0)   # 8h to 07:00
        app = self._app(t_in, rh_in)
        ceiling, base = asyncio.run(app._effective_ceiling(now))
        expected = self._bedroom_comfort_ceiling(t_in, rh_in, now)
        self.assertEqual(base, 23.0)
        self.assertEqual(ceiling, expected)
        self.assertLess(ceiling, 23.0)   # a humid night actually lowered it (non-trivial path)

    def test_dry_single_sleeper_is_the_knob(self):
        import asyncio
        # dry air, single sleeper (the deployed default) -> reduction 0 -> ceiling == knob
        now = datetime(2026, 7, 20, 23, 0)
        app = self._app(20.0, 35.0)
        ceiling, base = asyncio.run(app._effective_ceiling(now))
        self.assertEqual(ceiling, 23.0)
        self.assertEqual(base, 23.0)


class CoolRateLearning(unittest.TestCase):
    """The floor sensor only reports every ~30 min, so the rate is learned by accumulating
    engaged minutes against a reference reading and observing when the reading actually
    moves. 2026-07-29: a 1.0 C/h seed vs a real ~1.9 C/h made every plan ask double the
    minutes, over-booking price slots (the 11:00-start/11:31-stop the user asked about)."""

    def _app(self, **kw):
        app = make_app(**kw)
        app._save_state = lambda: None
        app.log = lambda *a, **k: None
        return app

    def test_seed_used_until_something_is_learned(self):
        app = self._app()
        self.assertEqual(app._cool_rate(), 1.0)

    def test_learns_the_real_rate_from_an_engaged_window(self):
        # the real incident: 24.6 -> 23.6 over ~31 engaged minutes = 1.94 C/h
        app = self._app()
        app._track_cool_rate(24.6, 0.0, True)      # sets the reference
        app._track_cool_rate(24.6, 15.0, True)     # no movement yet
        app._track_cool_rate(23.6, 16.0, True)     # 1.0C over 31 min
        self.assertAlmostEqual(app._cool_cph, 1.0 / (31.0 / 60.0), places=2)
        self.assertEqual(app._cool_cph_samples, 1)
        self.assertGreater(app._cool_rate(), 1.8)

    def test_window_shorter_than_minimum_does_not_learn(self):
        # a coarse sensor step divided by a tiny window would wildly overstate the rate
        app = self._app()
        app._track_cool_rate(24.6, 0.0, True)
        app._track_cool_rate(23.6, 5.0, True)      # 1.0C but only 5 engaged min
        self.assertIsNone(app._cool_cph)

    def test_not_cooling_resets_the_accumulator(self):
        # coast minutes mixed into the window would understate the rate
        app = self._app()
        app._track_cool_rate(24.6, 0.0, True)
        app._track_cool_rate(24.6, 15.0, True)
        app._track_cool_rate(24.4, 15.0, False)    # holding -> reset
        self.assertIsNone(app._rate_ref_floor)
        self.assertEqual(app._rate_engaged_min, 0.0)
        self.assertIsNone(app._cool_cph)

    def test_warming_past_the_reference_restarts_without_learning(self):
        app = self._app()
        app._track_cool_rate(23.0, 0.0, True)
        app._track_cool_rate(24.0, 30.0, True)     # warmed 1.0C above the reference
        self.assertIsNone(app._cool_cph)
        self.assertEqual(app._rate_ref_floor, 24.0)
        self.assertEqual(app._rate_engaged_min, 0.0)

    def test_observation_is_clamped_and_emad(self):
        app = self._app(cool_cph=2.0, cool_cph_samples=1)
        app._track_cool_rate(24.0, 0.0, True)
        app._track_cool_rate(20.0, 30.0, True)     # 8 C/h raw -> clamped to 4.0
        # EMA: 0.6*2.0 + 0.4*4.0 = 2.8
        self.assertAlmostEqual(app._cool_cph, 2.8, places=3)

    def test_none_floor_is_safe(self):
        app = self._app()
        app._track_cool_rate(None, 15.0, True)
        self.assertIsNone(app._cool_cph)


class ScheduleCommitment(unittest.TestCase):
    """Once cooling, don't abandon the run because the shrinking requirement dropped the
    slot we're running in out of the strict cheapest-N set (user 2026-07-29)."""

    NOW = datetime(2026, 7, 29, 11, 0)
    DEADLINE = datetime(2026, 7, 30, 0, 0)

    def _prices(self):
        # 11:00 hour is the marginal one: pricier than everything from 12:00 on.
        def price_at(dt):
            return 0.64 if dt.hour == 11 else 0.50
        return price_at

    def test_marginal_slot_is_dropped_when_not_already_cooling(self):
        app = make_app()
        # small need -> only the cheap 12:00+ slots are chosen, 11:00 is not
        cool_now, next_start, _, _, _ = app._schedule(
            self.NOW, self.DEADLINE, 60.0, self._prices(), already_cooling=False)
        self.assertFalse(cool_now)
        self.assertGreaterEqual(next_start.hour, 12)

    def test_same_situation_keeps_going_when_already_cooling(self):
        app = make_app()
        cool_now, _, _, _, _ = app._schedule(
            self.NOW, self.DEADLINE, 60.0, self._prices(), already_cooling=True)
        self.assertTrue(cool_now)

    def test_commitment_does_not_hold_through_a_genuinely_pricey_slot(self):
        # 11:00 at 1.50 vs 0.50 chosen -> far outside the margin -> stop and wait
        def pricey(dt):
            return 1.50 if dt.hour == 11 else 0.50
        app = make_app()
        cool_now, _, _, _, _ = app._schedule(
            self.NOW, self.DEADLINE, 60.0, pricey, already_cooling=True)
        self.assertFalse(cool_now)

    def test_zero_margin_turns_the_hysteresis_off(self):
        app = make_app(commit_price_margin=0.0)
        cool_now, _, _, _, _ = app._schedule(
            self.NOW, self.DEADLINE, 60.0, self._prices(), already_cooling=True)
        self.assertFalse(cool_now)

    def test_chosen_plan_is_unaffected_by_the_slack(self):
        # next_start/run_min still describe the STRICT cheapest-N plan
        app = make_app()
        _, ns_cold, run_cold, est_cold, _ = app._schedule(
            self.NOW, self.DEADLINE, 60.0, self._prices(), already_cooling=False)
        _, ns_hot, run_hot, est_hot, _ = app._schedule(
            self.NOW, self.DEADLINE, 60.0, self._prices(), already_cooling=True)
        self.assertEqual((ns_cold, run_cold, est_cold), (ns_hot, run_hot, est_hot))


class SchedulePlannedWindows(unittest.TestCase):
    """The chosen set is published as merged wall-clock windows so the dashboard bar can
    draw the real run / hold-for-price / resume plan (user 2026-07-30: the bar projected
    one contiguous block straight through the 19-21 peak the plan was skipping)."""

    NOW = datetime(2026, 7, 30, 17, 0)

    def test_contiguous_chosen_slots_merge_and_gaps_split(self):
        # 6 slots; cheapest three are k=0,1 (1.0) and k=4 (1.2) -> two windows with the
        # expensive 17:30-18:00 stretch held out between them.
        deadline = self.NOW + timedelta(minutes=90)
        prices = [1.0, 1.0, 5.0, 5.0, 1.2, 5.0]

        def price_at(dt):
            return prices[int((dt - self.NOW).total_seconds() // 900)]

        app = make_app()
        cool_now, next_start, run_min, _, windows = app._schedule(
            self.NOW, deadline, 45.0, price_at)
        self.assertTrue(cool_now)
        self.assertEqual(next_start, self.NOW)
        self.assertEqual(run_min, 45)
        self.assertEqual(windows, [
            [self.NOW, self.NOW + timedelta(minutes=30)],
            [self.NOW + timedelta(minutes=60), self.NOW + timedelta(minutes=75)],
        ])

    def test_no_work_publishes_no_windows(self):
        app = make_app()
        _, _, _, _, windows = app._schedule(
            self.NOW, self.NOW + timedelta(minutes=90), 0.0, lambda dt: 1.0)
        self.assertEqual(windows, [])


class PlanFloorLimit(unittest.TestCase):
    """The advisory must price the job the unit can ACTUALLY do. 2026-07-29: the plan sized a
    run down to min_temp 16C while the learned feasible floor was 20.5, showing "Run the AC
    ~12.5 kr" for a job the controller was really running to 20.2C (~5 kr)."""

    def test_falls_back_to_min_temp_without_enough_samples(self):
        app = make_app(feasible_floor=20.5, feasible_samples=1, feasible_min_samples=2)
        self.assertEqual(app._plan_floor_limit(), 16.0)

    def test_uses_the_learned_feasible_floor_with_the_probe(self):
        app = make_app(feasible_floor=20.5, feasible_samples=2, feasible_min_samples=2)
        self.assertAlmostEqual(app._plan_floor_limit(), 19.5, places=6)

    def test_never_below_min_temp(self):
        app = make_app(feasible_floor=15.0, feasible_samples=2, feasible_min_samples=2)
        self.assertEqual(app._plan_floor_limit(), 16.0)

    def test_no_learned_floor_uses_min_temp(self):
        app = make_app(feasible_floor=None, feasible_samples=0)
        self.assertEqual(app._plan_floor_limit(), 16.0)

    def test_capped_depth_shrinks_the_priced_deficit(self):
        # the real numbers: floor 23.6, E 26.6, limit 22.5, rise 0.59
        app = make_app(rise_frac=0.59, feasible_floor=20.5, feasible_samples=2,
                       feasible_min_samples=2)
        ideal = cm.calc_floor_target(26.6, 22.5, 0.59, 1.0, 16.0)
        capped = cm.calc_floor_target(26.6, 22.5, 0.59, 1.0, app._plan_floor_limit())
        self.assertLess(ideal, capped)                      # ideal digs deeper
        self.assertAlmostEqual(capped, 19.5, places=6)      # capped stops at the probed floor
        self.assertGreater((23.6 - ideal) - (23.6 - capped), 3.0)   # >3C of phantom job


class FinalizeNightFeasible(unittest.TestCase):
    """Learn the achieved floor minimum from EVERY cooling night, not only the rare
    saturation event (2026-07-29: saturation had fired ONCE in three weeks, so the planner
    sized every night off a single stale 20.5 while history held 12 good nights ~20.27)."""

    def _app(self, **kw):
        app = make_app(**kw)
        app._save_state = lambda: None
        app.log = lambda *a, **k: None
        return app

    def test_learns_from_a_normal_night(self):
        app = self._app(feasible_floor=None, feasible_samples=0,
                        night_floor_min=20.1, night_engaged_min=300.0)
        app._finalize_night()
        self.assertEqual(app._feasible_floor, 20.1)
        self.assertEqual(app._feasible_samples, 1)
        self.assertIsNone(app._night_floor_min)
        self.assertEqual(app._night_engaged_min, 0.0)

    def test_short_night_teaches_nothing(self):
        # 48 engaged min bottoms out on the clock, not on capacity
        app = self._app(feasible_floor=20.5, feasible_samples=1,
                        night_floor_min=21.4, night_engaged_min=48.0)
        app._finalize_night()
        self.assertEqual(app._feasible_floor, 20.5)
        self.assertEqual(app._feasible_samples, 1)

    def test_saturation_already_learned_tonight_is_not_double_counted(self):
        app = self._app(feasible_floor=20.5, feasible_samples=1,
                        night_floor_min=20.0, night_engaged_min=300.0,
                        learned_tonight=True)
        app._finalize_night()
        self.assertEqual(app._feasible_samples, 1)      # unchanged
        self.assertFalse(app._learned_tonight)          # reset for the next night

    def test_missing_reading_is_safe(self):
        app = self._app(night_floor_min=None, night_engaged_min=300.0)
        app._finalize_night()                            # must not raise
        self.assertEqual(app._night_engaged_min, 0.0)

    def test_converges_to_the_typical_night_not_the_deepest(self):
        # the real 12-night spread; the user reports the deep nights feel no colder, so the
        # learner must land on the central value, never chase 19.2.
        nights = [21.0, 20.3, 21.2, 19.6, 19.9, 20.4, 20.0, 19.9, 20.2, 19.2]
        app = self._app(feasible_floor=None, feasible_samples=0)
        for fm in nights:
            app._night_floor_min = fm
            app._night_engaged_min = 300.0
            app._learned_tonight = False
            app._finalize_night()
        self.assertEqual(app._feasible_samples, len(nights))
        self.assertGreater(app._feasible_floor, 19.8)    # well above the deepest 19.2
        self.assertLess(app._feasible_floor, 20.8)       # and below the shallowest 21.2

    def test_tick_accumulates_only_engaged_time(self):
        app = self._app(night_floor_min=None, night_engaged_min=0.0)
        # mimic the eval-loop accumulation
        for floor, engaged in ((22.0, 15.0), (21.0, 15.0), (20.5, 0.0), (24.0, 15.0)):
            if engaged > 0 and floor is not None:
                app._night_engaged_min += engaged
                app._night_floor_min = (floor if app._night_floor_min is None
                                        else min(app._night_floor_min, floor))
        self.assertEqual(app._night_engaged_min, 45.0)   # the 0-engaged tick didn't count
        self.assertEqual(app._night_floor_min, 21.0)     # and the warm tick didn't raise it


class FeasibleProbe(unittest.TestCase):
    """Probe depth below the learned feasible floor (user 2026-07-29: "probe deeper... but
    balance it"). Balanced by construction -- max(physics target, feasible - probe) means the
    probe can only chase depth the night ACTUALLY needs."""

    def _app(self, **kw):
        app = make_app(**kw)
        app.log = lambda *a, **k: None
        return app

    def test_probe_reaches_below_the_learned_floor(self):
        app = self._app(feasible_floor=20.3, feasible_samples=6, feasible_probe_c=1.0)
        # hot night: physics wants 16.0, so the probe governs -> 20.3 - 1.0
        got = app._reach_target(datetime(2026, 7, 29, 20, 0), 16.0, False)
        self.assertAlmostEqual(got, 19.3, places=6)

    def test_probe_never_chases_depth_the_night_does_not_need(self):
        # THE balance guarantee: a mild night whose physics target is 21.5 stops at 21.5,
        # never at the deeper 19.3 the probe would allow.
        app = self._app(feasible_floor=20.3, feasible_samples=6, feasible_probe_c=1.0)
        got = app._reach_target(datetime(2026, 7, 29, 20, 0), 21.5, False)
        self.assertAlmostEqual(got, 21.5, places=6)

    def test_saturation_still_wins(self):
        # priority 1: a floor proven not to move tonight overrides the probe
        app = self._app(feasible_floor=20.3, feasible_samples=6, feasible_probe_c=1.0)
        app._sat_min = 20.8
        got = app._reach_target(datetime(2026, 7, 29, 20, 0), 16.0, True)
        self.assertAlmostEqual(got, 20.8, places=6)

    def test_plan_floor_limit_uses_the_same_probe(self):
        # the advisory plan must size the same job the controller will run
        app = self._app(feasible_floor=20.3, feasible_samples=6, feasible_probe_c=1.0)
        self.assertAlmostEqual(app._plan_floor_limit(), 19.3, places=6)


class CoolRateHeadroomGate(unittest.TestCase):
    """Crawl segments near the feasible limit measure the WALL, not the machine
    (2026-07-29: they dragged the learned rate 1.68 -> 0.44 C/h and the next morning a
    4.4C job priced as ~600 min, forcing a run through morning prices)."""

    def _app(self):
        app = make_app(feasible_floor=20.3, feasible_samples=6)
        app.rate_learn_min_headroom = 0.7
        app._cool_cph = 1.68
        app._cool_cph_samples = 1
        app._rate_ref_floor = None
        app._rate_engaged_min = 0.0
        app._save_state = lambda: None
        app.log = lambda *a, **k: None
        return app

    def test_crawl_near_the_limit_teaches_nothing(self):
        app = self._app()
        # floor 20.8 is only 0.5 above the 20.3 limit -> every sample skipped
        app._track_cool_rate(20.8, 0.0, True)     # anchor attempt
        for _ in range(4):
            app._track_cool_rate(20.7, 60.0, True)
        self.assertEqual(app._cool_cph, 1.68)      # unchanged
        self.assertEqual(app._cool_cph_samples, 1)

    def test_far_from_the_limit_still_learns(self):
        app = self._app()
        app._track_cool_rate(23.5, 0.0, True)      # anchor (3.2 above limit)
        app._track_cool_rate(22.6, 32.0, True)     # 0.9C in 32 min ~ 1.69 C/h
        self.assertEqual(app._cool_cph_samples, 2)
        self.assertGreater(app._cool_cph, 1.5)

    def test_no_learned_floor_keeps_old_behavior(self):
        app = self._app()
        app._feasible_floor = None
        app._track_cool_rate(20.8, 0.0, True)
        app._track_cool_rate(20.2, 40.0, True)     # 0.6C in 40 min ~ 0.9 C/h, allowed
        self.assertEqual(app._cool_cph_samples, 2)


class GetForecastStaleReuse(unittest.TestCase):
    """weather.get_forecasts intermittently returns an empty payload with NO error at all
    (found 2026-07-30: the weather model published no prediction on 7 of 11 mornings).
    _get_forecast now treats an empty parse exactly like a fetch exception: both mark
    _fc_fail_at and reuse the last good forecast (while under wm_forecast_reuse_h old)
    instead of a hard None, backed by a 120s fail-backoff (one broken tick can't fire
    repeated service calls -- 4+ methods call this every eval tick) and a 30-min rate
    limit on the empty-payload WARNING (energidataservice's "empty dataset!" log spam is
    the cautionary tale)."""

    EMPTY_RESP = {"result": {"response": {"weather.forecast_home": {"forecast": []}}}}
    GOOD_RESP = {"result": {"response": {"weather.forecast_home": {"forecast": [
        {"datetime": "2026-07-30T08:00:00+00:00", "temperature": 20.0, "cloud_coverage": 10},
        {"datetime": "2026-07-30T09:00:00+00:00", "temperature": 21.0, "cloud_coverage": 20},
    ]}}}}

    def _app(self, reuse_h=6.0, responses=()):
        """responses: successive call_service return values, consumed one per real
        service call (never re-used, so a test can assert exactly how many were made)."""
        app = make_app()
        app.weather_forecast_entity = "weather.forecast_home"
        app.wm_forecast_reuse_h = reuse_h
        app._queue = list(responses)
        app._service_calls = []

        async def call_service(service, **kw):
            app._service_calls.append((service, kw))
            return app._queue.pop(0)
        app.call_service = call_service

        async def get_now():
            return datetime(2026, 7, 30, 8, 0)
        app.get_now = get_now

        app._log_calls = []

        def log(msg, level="INFO"):
            app._log_calls.append((msg, level))
        app.log = log
        return app

    @staticmethod
    def _warnings(app):
        return [msg for msg, level in app._log_calls if level == "WARNING"]

    @staticmethod
    def _run(app, now):
        import asyncio
        return asyncio.run(app._get_forecast(now))

    def test_empty_payload_reuses_cache_within_reuse_window(self):
        now = datetime(2026, 7, 30, 8, 0)
        app = self._app(reuse_h=6.0, responses=[self.EMPTY_RESP])
        stale = [{"dt": datetime(2026, 7, 30, 6, 0), "temp": 19.0, "cloud": None}]
        app._fc_cache = stale
        app._fc_cache_at = now - timedelta(hours=2)   # stale but within the 6h reuse window

        result = self._run(app, now)

        self.assertEqual(result, stale)
        self.assertEqual(app._fc_served_age_min, 120)
        self.assertEqual(len(app._service_calls), 1)
        warnings = self._warnings(app)
        self.assertEqual(len(warnings), 1)
        self.assertIn("reusing forecast from 120 min ago", warnings[0])
        self.assertEqual(app._fc_fail_at, now)

    def test_empty_payload_past_reuse_window_returns_none(self):
        now = datetime(2026, 7, 30, 8, 0)
        app = self._app(reuse_h=6.0, responses=[self.EMPTY_RESP])
        app._fc_cache = [{"dt": datetime(2026, 7, 29, 12, 0), "temp": 18.0, "cloud": None}]
        app._fc_cache_at = now - timedelta(hours=7)   # older than the 6h reuse window

        result = self._run(app, now)

        self.assertIsNone(result)
        self.assertIsNone(app._fc_served_age_min)
        warnings = self._warnings(app)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no cached forecast to reuse", warnings[0])

    def test_backoff_skips_the_service_call_within_120s(self):
        now0 = datetime(2026, 7, 30, 8, 0)
        app = self._app(reuse_h=6.0, responses=[self.EMPTY_RESP])
        self._run(app, now0)   # first (real) failure -- one service call, one warning
        self.assertEqual(len(app._service_calls), 1)
        self.assertEqual(len(self._warnings(app)), 1)

        # 30s later -- well within the 120s backoff: must not call the service again.
        result = self._run(app, now0 + timedelta(seconds=30))

        self.assertEqual(len(app._service_calls), 1)     # unchanged -- no new call at all
        self.assertIsNone(result)                          # nothing cached to reuse either
        self.assertEqual(len(self._warnings(app)), 1)      # backoff path stays silent

    def test_warning_rate_limited_across_repeated_empty_fetches(self):
        now0 = datetime(2026, 7, 30, 8, 0)
        app = self._app(reuse_h=6.0, responses=[self.EMPTY_RESP, self.EMPTY_RESP])
        self._run(app, now0)
        self.assertEqual(len(app._service_calls), 1)
        self.assertEqual(len(self._warnings(app)), 1)

        # 5 min later: past the 120s backoff, so a real second fetch IS attempted...
        result = self._run(app, now0 + timedelta(minutes=5))

        self.assertEqual(len(app._service_calls), 2)       # backoff had already expired
        self.assertIsNone(result)
        # ...but the 30-min rate limit still suppresses a second WARNING.
        self.assertEqual(len(self._warnings(app)), 1)

    def test_successful_fetch_resets_failure_state_and_caches(self):
        now = datetime(2026, 7, 30, 8, 0)
        app = self._app(responses=[self.GOOD_RESP])
        # Outside the 120s backoff window -- a real fetch must still be attempted, not
        # short-circuited by the backoff skip (that's a separate behaviour, covered by
        # test_backoff_skips_the_service_call_within_120s).
        app._fc_fail_at = now - timedelta(minutes=10)
        app._fc_warned_at = now - timedelta(minutes=10)

        result = self._run(app, now)

        self.assertEqual(len(result), 2)
        self.assertEqual(app._fc_served_age_min, 0)
        self.assertIsNone(app._fc_fail_at)
        self.assertIsNone(app._fc_warned_at)
        self.assertEqual(app._fc_cache_at, now)


class CoolIntervalsToday(unittest.TestCase):
    """Day-history for the dashboard bar (2026-07-30): which stretches the AC actually
    spent ENGAGED today (cooling or stall-burping), tracked from a status TRANSITION at
    the single publish site (_publish) against the previously PUBLISHED status
    (_last_pub_status), not the internal want/decision."""

    def test_engaged_statuses_are_exactly_cooling_and_burping(self):
        # Locks the dashboard's running definition -- dry_run's "cooling_dryrun" is
        # deliberately excluded (a rehearsal run is not a real-run history entry).
        self.assertEqual(sc.SmartCooling.ENGAGED_STATUSES, {"cooling", "burping"})

    def _app(self, now):
        app = make_app()
        app.status_entity = "sensor.smart_cooling_status"
        app._set_state_calls = []

        async def set_state(entity, **kw):
            app._set_state_calls.append((entity, kw))
        app.set_state = set_state

        async def get_now():
            return now
        app.get_now = get_now
        return app

    @staticmethod
    def _publish(app, status, now):
        import asyncio

        async def get_now():
            return now
        app.get_now = get_now
        asyncio.run(app._publish(status, "reason", {}))
        return app._set_state_calls[-1][1]["attributes"]["cool_intervals_today"]

    def test_transition_into_engaged_opens_an_interval(self):
        app = self._app(datetime(2026, 7, 30, 10, 0))
        self._publish(app, "idle", datetime(2026, 7, 30, 10, 0))
        self.assertEqual(app._cool_intervals, [])

        now2 = datetime(2026, 7, 30, 10, 15)
        self._publish(app, "cooling", now2)
        self.assertEqual(app._cool_intervals, [[now2.isoformat(), None]])

    def test_transition_out_of_engaged_closes_the_interval(self):
        app = self._app(datetime(2026, 7, 30, 10, 0))
        now = datetime(2026, 7, 30, 10, 0)
        self._publish(app, "cooling", now)
        now2 = datetime(2026, 7, 30, 10, 30)
        self._publish(app, "idle", now2)
        self.assertEqual(app._cool_intervals, [[now.isoformat(), now2.isoformat()]])

    def test_repeated_engaged_status_does_not_fragment_the_interval(self):
        app = self._app(datetime(2026, 7, 30, 10, 0))
        now = datetime(2026, 7, 30, 10, 0)
        self._publish(app, "cooling", now)
        self._publish(app, "burping", now + timedelta(minutes=5))
        self._publish(app, "cooling", now + timedelta(minutes=8))
        self.assertEqual(len(app._cool_intervals), 1)
        self.assertEqual(app._cool_intervals[0][0], now.isoformat())
        self.assertIsNone(app._cool_intervals[0][1])

    def test_self_heals_a_dangling_open_interval(self):
        # An app/AppDaemon restart mid-run: _last_pub_status resets to None on startup,
        # but a reloaded _cool_intervals can still have last night's run left open.
        now = datetime(2026, 7, 30, 10, 0)
        app = self._app(now)
        app._cool_intervals = [["2026-07-29T23:00:00", None]]
        app._last_pub_status = None
        self._publish(app, "idle", now)
        self.assertEqual(app._cool_intervals, [["2026-07-29T23:00:00", now.isoformat()]])

    def test_restart_while_still_engaged_does_not_stack_a_second_open_interval(self):
        # Same restart, but the very next tick is STILL "cooling" -- must not fragment the
        # already-open interval into two simultaneously-open entries.
        now = datetime(2026, 7, 30, 10, 0)
        app = self._app(now)
        app._cool_intervals = [["2026-07-29T23:00:00", None]]
        app._last_pub_status = None
        self._publish(app, "cooling", now)
        self.assertEqual(app._cool_intervals, [["2026-07-29T23:00:00", None]])

    def test_open_interval_survives_the_24h_prune(self):
        now = datetime(2026, 7, 30, 10, 0)
        app = self._app(now)
        old_open = ["2026-07-27T10:00:00", None]
        app._cool_intervals = [old_open]
        app._last_pub_status = "cooling"
        self._publish(app, "cooling", now)   # still engaged -- no transition either way
        self.assertEqual(app._cool_intervals, [old_open])

    def test_closed_intervals_older_than_24h_are_pruned(self):
        now = datetime(2026, 7, 30, 10, 0)
        app = self._app(now)
        stale_closed = ["2026-07-28T10:00:00", "2026-07-28T10:30:00"]    # end >24h old
        recent_closed = ["2026-07-29T20:00:00", "2026-07-29T20:30:00"]   # end <24h old
        app._cool_intervals = [stale_closed, recent_closed]
        app._last_pub_status = "idle"
        self._publish(app, "idle", now)
        self.assertEqual(app._cool_intervals, [recent_closed])

    def test_published_attribute_matches_internal_state(self):
        now = datetime(2026, 7, 30, 10, 0)
        app = self._app(now)
        published = self._publish(app, "cooling", now)
        self.assertEqual(published, app._cool_intervals)


class CoolIntervalsPersistence(unittest.TestCase):
    """cool_intervals round-trips through the real _save_state/_load_state; the loader
    validates the shape (2-item [start, end_or_None] lists, ISO-parseable) and drops
    anything malformed rather than crash, mirroring the defensive style _load_state
    already uses for its other fields."""

    def _real_state_app(self, state_file):
        app = make_app()
        app.state_file = state_file
        # _save_state/_load_state also touch these two; make_app doesn't set them (only
        # tests that exercise lightout/rescue directly do), so supply them here.
        app._lightout = None
        app._rescue_notified_date = None
        del app._save_state   # make_app stubs this to a no-op -- restore the real method
        return app

    def _write(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f)

    def _tmp_path(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_round_trips_through_save_and_load(self):
        path = self._tmp_path()
        app = self._real_state_app(path)
        app._cool_intervals = [["2026-07-29T22:00:00+02:00", "2026-07-30T00:15:00+02:00"],
                               ["2026-07-30T09:00:00+02:00", None]]
        app._save_state()

        app2 = self._real_state_app(path)
        app2._cool_intervals = []
        app2._load_state()

        self.assertEqual(app2._cool_intervals, app._cool_intervals)

    def test_missing_key_keeps_the_pre_load_default(self):
        path = self._tmp_path()
        self._write(path, {})
        app = self._real_state_app(path)
        app._cool_intervals = []
        app._load_state()
        self.assertEqual(app._cool_intervals, [])

    def test_malformed_entries_are_dropped_not_crashed(self):
        path = self._tmp_path()
        self._write(path, {"cool_intervals": [
            ["2026-07-29T22:00:00+02:00", "2026-07-30T00:15:00+02:00"],   # valid, closed
            ["2026-07-30T09:00:00+02:00", None],                          # valid, open
            ["not-a-date", None],                                         # bad start
            ["2026-07-30T09:00:00+02:00", "not-a-date"],                  # bad end
            ["2026-07-30T09:00:00+02:00"],                                # wrong length
            "not-a-list",                                                 # wrong type
            {"start": "x"},                                               # wrong type
        ]})
        app = self._real_state_app(path)
        app._cool_intervals = []

        app._load_state()

        self.assertEqual(app._cool_intervals, [
            ["2026-07-29T22:00:00+02:00", "2026-07-30T00:15:00+02:00"],
            ["2026-07-30T09:00:00+02:00", None],
        ])

    def test_non_list_value_drops_to_empty(self):
        path = self._tmp_path()
        self._write(path, {"cool_intervals": "not-a-list-at-all"})
        app = self._real_state_app(path)
        app._cool_intervals = ["should be replaced"]
        app._load_state()
        self.assertEqual(app._cool_intervals, [])


if __name__ == "__main__":
    unittest.main()


class ActuationGrounding(unittest.TestCase):
    """One reality (2026-08-06): the armed controller's equilibrium is capped at the sleep
    plan's grounded value. Fri Jul 31 the plan said windows/nothing all evening while the
    armed path burned 594 engaged minutes chasing the raw kitchen-proxy E."""

    def _app(self, **kw):
        app = make_app(**kw)
        app.interval_min = 15
        # Pin the live configuration: model computes in SHADOW, so actuation E = e_legacy.
        app.weather_model_enabled = True
        app.wm_shadow = True
        return app

    def test_fresh_plan_caps_the_legacy_equilibrium(self):
        app = self._app()
        now = datetime(2026, 7, 31, 18, 0)
        app._last_plan = {"rec": "windows", "equilibrium": 22.6, "at": now}
        self.assertAlmostEqual(app._actuation_equilibrium(26.0, 24.5, now), 22.6)

    def test_never_deepens_a_cooler_raw_equilibrium(self):
        app = self._app()
        now = datetime(2026, 7, 31, 18, 0)
        app._last_plan = {"rec": "ac", "equilibrium": 26.0, "at": now}
        self.assertAlmostEqual(app._actuation_equilibrium(27.0, 23.0, now), 23.0)

    def test_stale_stash_leaves_e_untouched(self):
        app = self._app()
        now = datetime(2026, 7, 31, 18, 0)
        app._last_plan = {"rec": "windows", "equilibrium": 20.0,
                          "at": now - timedelta(minutes=60)}
        self.assertAlmostEqual(app._actuation_equilibrium(26.0, 24.5, now), 24.5)

    def test_kill_switch_restores_old_behaviour(self):
        app = self._app(ground_actuation=False)
        now = datetime(2026, 7, 31, 18, 0)
        app._last_plan = {"rec": "windows", "equilibrium": 20.0, "at": now}
        self.assertAlmostEqual(app._actuation_equilibrium(26.0, 24.5, now), 24.5)

    def test_shadow_off_grounds_the_weather_equilibrium_too(self):
        app = self._app()
        app.weather_model_enabled = True
        app.wm_shadow = False
        now = datetime(2026, 7, 31, 18, 0)
        app._last_plan = {"rec": "windows", "equilibrium": 22.0, "at": now}
        self.assertAlmostEqual(app._actuation_equilibrium(26.0, 24.5, now), 22.0)


class VentTauSelection(unittest.TestCase):
    """Direct venting (measured 7 h) needs the bedroom window open AND the curtain aside;
    everything else is the indirect half-speed path (user 2026-08-07: 25C at wake beside a
    fully open window, curtain across)."""

    def test_open_window_and_open_curtain_is_direct(self):
        app = make_app()
        self.assertEqual(app._vent_tau(True, 0.0, False), app.vent_tau_h)
        # 38 is this blind's fully-open park position (user 2026-08-07) - inside the 45 line.
        self.assertEqual(app._vent_tau(True, 38.0, False), app.vent_tau_h)

    def test_solar_shade_position_counts_as_closed(self):
        # The shade parks at 77 (bottom-up, inverted: 100 = closed) - that chokes airflow.
        app = make_app()
        self.assertEqual(app._vent_tau(True, 77.0, False), app.vent_tau_indirect_h)

    def test_open_door_is_the_middle_tier(self):
        app = make_app()
        self.assertEqual(app._vent_tau(False, 0.0, True), app.vent_tau_door_h)
        self.assertEqual(app._vent_tau(True, 77.0, True), app.vent_tau_door_h)

    def test_all_shut_is_conduction_only(self):
        app = make_app()
        self.assertEqual(app._vent_tau(False, 0.0, False), app.vent_tau_indirect_h)

    def test_unreadable_curtain_errs_warm(self):
        app = make_app()
        self.assertEqual(app._vent_tau(True, None, False), app.vent_tau_indirect_h)
