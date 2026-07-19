from __future__ import annotations

import sys
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
        cool_now, _, run_min, _ = app._schedule(
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
        cool_now, _, _, _ = app._schedule(
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
        # ideal 18.3 is below the learned wall (probed 0.3C deep) -> capped warmer
        self.assertEqual(app._reach_target(now, target=18.3, saturated=False), 20.2)

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
        self.assertEqual(app._reach_target(now, target=18.0, saturated=False), 19.7)


class WindowOpen(unittest.TestCase):
    """Regression for the 2026-07-16 Zigbee flaps: binary_sensor.bathroom_window_contact
    dropped off 5x that day (~70 s unavailable->unknown->on blips, battery full), and the
    old fail-closed read (state == "on") turned each dropout into "Bathroom window closed
    -- open it" with the AC off mid-cheap-slot and a 10-min anti-short-cycle lockout on
    the way back. Only an explicit "off" may read as closed; a genuinely closed window is
    backstopped by the bathroom_hard_max guard within minutes."""

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


class CondenserHazard(unittest.TestCase):
    """Regression for the 2026-07-17->18 incident (root-caused via deep-reasoner): the
    bathroom hard cap lived only inside the ARMED decision chain, so a disarm (correct,
    one-shot "AC off") followed 4s later by something OUTSIDE this app re-enabling the
    compressor ran completely unwatched for ~9h -- bathroom hit 39.8C, nearly 7C past
    the 33C cap, before the next armed eval's hard cap finally caught it. This predicate
    must fire regardless of arm state whenever the unit is genuinely running hot."""

    HARD_MAX = 33.0

    def test_running_hot_is_a_hazard(self):
        self.assertTrue(sc.SmartCooling._condenser_hazard(True, "cool", 39.8, self.HARD_MAX))

    def test_exactly_at_cap_is_a_hazard(self):
        self.assertTrue(sc.SmartCooling._condenser_hazard(True, "cool", 33.0, self.HARD_MAX))

    def test_below_cap_is_not_a_hazard(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "cool", 32.9, self.HARD_MAX))

    def test_genuinely_off_is_not_a_hazard_even_if_hot(self):
        # nothing to force off - a hot bathroom with the unit already off isn't this guard's job
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "off", 39.8, self.HARD_MAX))

    def test_not_deployed_is_not_a_hazard(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(False, "cool", 39.8, self.HARD_MAX))

    def test_unavailable_climate_is_not_a_hazard(self):
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "unavailable", 39.8, self.HARD_MAX))

    def test_missing_bath_reading_fails_closed(self):
        # matches the existing armed backleak_hard shape: a dropped sensor doesn't force anything
        self.assertFalse(sc.SmartCooling._condenser_hazard(True, "cool", None, self.HARD_MAX))

    def test_dry_mode_can_also_be_a_hazard(self):
        # dry still runs the compressor/condenser, not just cool
        self.assertTrue(sc.SmartCooling._condenser_hazard(True, "dry", 39.8, self.HARD_MAX))

    def test_fan_only_can_also_be_a_hazard(self):
        # conservative on purpose: force off rather than assume a burp explains a 39.8C reading
        self.assertTrue(sc.SmartCooling._condenser_hazard(True, "fan_only", 39.8, self.HARD_MAX))


if __name__ == "__main__":
    unittest.main()
