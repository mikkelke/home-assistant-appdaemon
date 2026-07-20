"""
Smart Cooling v2 -- closed-loop pre-cool for the bedroom PortaSplit AC.

Opt-in: Arm = input_boolean.smart_cooling, and the unit must be deployed (climate available).
The job: gently pre-cool the FURNITURE/floor "battery" in the cheapest hours, deep enough that
the sealed room (AC removed) coasts with the floor-to-mid SLEEPING ZONE <= the max temperature
(default 23C) for ~8 h.

Closed-loop, not predictive: the FLOOR sensor is the feedback. Each tick it compares the floor to a
CALCULATED target (no knob) and schedules the remaining cooling into the cheapest hours, re-deciding
every tick so drift self-corrects. The target comes from a self-learning warm-up indicator -- every
sealed sleep window IS an 8 h AC-off coast, so we learn "from floor F0 the zone closed fraction r of
the gap to its equilibrium" and invert it.

No bedtime knob (user, 2026-07-15): real bedtime varies 22:00-01:00, so a fixed clock setting was
either wrong or a chore to keep updated. Two independent things used to share that one knob:
  - WHEN TO STOP: the user clicks ac_removed_entity right before physically unplugging the unit -
    that IS lights-out, whatever the clock says. The press stamps the coast-learning baseline
    (_stash_lightout), stops the compressor gracefully ahead of the power pull, and DISARMS the
    master for the night (user design 2026-07-17: the unplug is the real seal; a still-armed
    planner beside a still-plugged unit would resume cooling next to the sleeping user).
    Re-arming is the existing morning ritual.
  - HOW FAR AHEAD TO SHOP FOR CHEAP POWER: two tiers (user, 2026-07-16). GUARANTEED: until
    22:00 (the earliest plausible bedtime) the load-bearing plan must fit BEFORE 22:00 -- later
    slots can be erased by the AC-removed press, and a top-up starting at 22:08 is a noisy
    machine next to someone trying to sleep (see _plan_deadline). BONUS: from 22:00 until the
    AC-removed press the user is evidently still up, so keep improving the night against the
    wider _deadline horizon (midnight cap -- never bank on slots past it, user 2026-07-15 --
    then a rolling 1h maintenance window through 06:00; the raw midnight cap once shut the AC
    off at 23:52 mid-deficit, hence the 1h floor). Past midnight the AIM also changes, not just
    the horizon (user, 2026-07-17): 00-06 is reliably cheap and sealing is imminent, so there's
    no daytime decay to out-leak extra depth -- chase the hardware floor, not just the physics
    minimum, bounded only by tonight's real saturation (see _reach_target).

Stall-breaker (2026-07-15): the unit regulates off its own intake sensor, which sits in its cold
outflow pool -- it parks at ~300 W "idle" with the floor still degrees above target, and no fan
mode fixes that (low/high/full all measured stalled). When a tick sees idle + real deficit, a
short fan_only burp lets the intake read true room air and the compressor restarts hard.

Occupied quiet mode (2026-07-16/19): someone in bed changes what "gentle" means. Burps are
skipped entirely (the silence->800W restart is the annoying part, not the parked crawl -- the
Remove press ends the night soon anyway); active cooling switches from cool_fan_mode to the
quieter cool_fan_quiet_mode (2026-07-19: "watching TV in bed -> cool, just less noisy" -- still
cooling, trading some delivered rate for noise, same trade the user asked for). Same
_bed_occupied() signal drives both.

Feasibility cap (2026-07-15): the ideal target can sit below what the unit + apartment heat can
physically deliver; an unclosable deficit would inflate minutes_needed until the scheduler goes
time-constrained and grinds 600 W through the evening price peak chasing the impossible (user:
"don't we lose some of what we wanted to accomplish?"). Sustained engaged time with zero floor
progress = tonight's feasible floor: stop paying, learn it (EMA across nights), and plan future
nights against the feasible depth -- probed slightly deeper so milder nights can re-teach us.

Knobs you ever touch: Arm + max night temperature (a default is fine, depth is calculated); AC
removed. dry_run makes every AC command a no-op. The bathroom (the condenser's heat dump) is
watched price-aware: its door is kept sealed so a warm bathroom only costs condenser efficiency
(~2-3%/C, far less than the cheap-vs-peak price spread) -- we push through warm-bathroom cheap
slots and let venting happen in hours we'd hold anyway; only impaired venting stops us, judged
by how far the bathroom sits ABOVE OUTDOOR (bathroom_delta_max), not an absolute temperature
(user, 2026-07-19: "it is all about how warm it is compared to the outside temperature" --
validated against 19 days of real data: legitimate operation never exceeded +9.9C above outdoor,
while the incident below was already past +11.4C within an hour of onset and reached +21.2C at
its worst).

Condenser safety watchdog (found 2026-07-19, root-caused via deep-reasoner): the delta guard
above lived ONLY inside the armed decision chain, so "OFF = HANDS OFF" accidentally meant "and
stop watching the condenser too." Incident: disarmed correctly at 00:19:28 (2026-07-17), but 4s
later something OUTSIDE this app put the AC back into cool; disarmed = we never touch it again,
so nobody was watching for the ~9h it then ran into a barely-vented bathroom, which hit 39.8C
(+21.2C above outdoor) before the next ARMED eval's guard finally caught it -- 6-10h later than
a delta check would have. _condenser_hazard() now fires the same venting-impaired check
regardless of arm state -- it only ever forces a stop for an actual hazard, never routine
planning, so it can't fight a genuine manual session, only an actually-hazardous one. _evaluate
is now also serialized (asyncio.Lock): the re-arm that morning triggered three concurrent
evaluations that raced and duplicated actuation.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import asyncio
from datetime import datetime, timedelta
import json
import math
from typing import Optional


class SmartCooling(hass.Hass):
    def initialize(self) -> None:
        a = self.args.get
        # --- entities (the four bedroom points + neighbours + control) ---
        self.climate_entity = a("climate_entity", "climate.air_conditioner_thermostat")
        self.floor_sensor = a("floor_sensor", "sensor.bedroom_floor_thermometer_temperature")  # MASS battery + control + sleeping-zone floor
        self.mid_sensor = a("mid_sensor", "sensor.bedroom_temperature")                         # mid wall by kitchen door = top of sleeping zone
        self.ceiling_sensor = a("ceiling_sensor", "sensor.bedroom_presence_temperature")        # AC blows cold up here -> delivery check
        self.ac_sensor = a("ac_sensor", "sensor.air_conditioner_indoor_temperature")            # at the unit -> is cold flowing
        self.bathroom_sensor = a("bathroom_temp_sensor", "sensor.bathroom_temperature")         # condenser dump / back-leak guard
        self.kitchen_sensor = a("kitchen_sensor", "sensor.kitchen_temperature")                 # neighbour wall (headwind)
        self.outdoor_sensor = a("outdoor_temp_sensor", "sensor.gw2000a_outdoor_temperature")    # condenser hazard reference (see bathroom_delta_max)
        self.vent_window = a("vent_window_sensor", "binary_sensor.bathroom_window_contact")
        self.price_entity = a("price_entity", "sensor.energi_data_service")
        self.enable_entity = a("enable_entity", "input_boolean.smart_cooling")
        # --- the only user knobs: Arm (above) + AC removed; max temperature is an optional default ---
        # Click right before physically removing the AC - the true lights-out moment, whatever
        # the clock says. Replaces the old fixed-clock bedtime cutoff entirely (see module docstring).
        self.ac_removed_entity = a("ac_removed_entity", "input_boolean.smart_cooling_ac_removed")
        self.night_ceiling_entity = a("night_ceiling_entity", "input_number.smart_cooling_night_ceiling")
        # Humidity-aware ceiling from the comfort middle layer (sensor.bedroom_comfort).
        self.comfort_entity = a("comfort_entity", "sensor.bedroom_comfort")
        self.comfort_max_reduction = float(a("comfort_max_reduction", 1.5))
        # --- fixed params (not user-facing) ---
        self.default_ceiling = float(a("default_night_ceiling", 23.0))
        self.min_temp = float(a("min_temp", 16.0))            # hardware floor; never drive below
        self.sleep_hours = float(a("sleep_hours", 8.0))
        self.floor_cool_cph = float(a("floor_cool_cph", 1.0))     # measured ~1 C/h floor/mass cool rate
        self.zone_offset = float(a("zone_offset", 1.0))           # mid wall sits ~this above the floor
        self.person_offset = float(a("person_offset", 0.5))      # sleeper lifts the equilibrium a touch
        self.default_rise_frac = float(a("default_rise_frac", 0.7))  # conservative gap-fraction closed in the window
        # --- weather-driven equilibrium estimator (verified Model D; SHADOW rollout) ---
        # Predicts the apartment's daytime peak the sealed room coasts toward, from
        # measured+forecast solar, forecast outdoor peak, a resting baseline, and one day of
        # thermal-mass memory (yesterday's kitchen peak). Ships behind two flags:
        # weather_model_enabled is the master kill-switch back to the legacy proxy; wm_shadow
        # computes+publishes the weather E every tick but keeps the LEGACY value DRIVING
        # actuation, so predicted-vs-actual can be validated for 1-2 weeks before flipping.
        # Every degradation path (missing solar, failed forecast, unseeded memory, any
        # exception) falls back to E_legacy, and E is floored at E_legacy - wm_nowcast_relief
        # so a broken model can never crater the equilibrium (a too-warm room is the one
        # failure the whole system exists to prevent). Coefficients are single-warm-season
        # (Apr-Jul); the 24C vent knee is a hand-picked CONSERVATIVE constant, not a tuned
        # parameter -- revisit with more hot-day data. See _weather_equilibrium.
        self.weather_model_enabled = bool(a("weather_model_enabled", False))
        self.wm_shadow = bool(a("wm_shadow", True))
        self.wm_b0 = float(a("wm_b0", 15.797))
        self.wm_b_solar = float(a("wm_b_solar", 0.0162))
        self.wm_b_vent = float(a("wm_b_vent", 0.198))
        self.wm_vent_knee = float(a("wm_vent_knee", 24.0))
        self.wm_b_prev = float(a("wm_b_prev", 0.287))
        self.wm_safety_margin = float(a("wm_safety_margin", 0.0))
        self.wm_nowcast_relief = float(a("wm_nowcast_relief", 1.5))
        self.wm_clearsky_peak = float(a("wm_clearsky_peak", 700.0))
        self.wm_cloud_atten = float(a("wm_cloud_atten", 0.75))
        self.wm_peak_hour = int(a("wm_peak_hour", 15))
        self.solar_sensor = a("solar_sensor", "sensor.gw2000a_solar_radiation")
        self.weather_forecast_entity = a("weather_forecast_entity", "weather.forecast_home")
        self.sun_entity = a("sun_entity", "sun.sun")
        # Condenser-room temps: above `bathroom_max` the condenser loses ~2-3%/C efficiency
        # (worth flagging, NOT worth skipping a cheap slot for -- door is kept sealed, so no
        # real back-leak; user 2026-07-16). `bathroom_delta_max` is the real stop condition:
        # not an absolute temperature, but how far the bathroom sits ABOVE outdoor (user,
        # 2026-07-19: "it is all about how warm it is compared to the outside temperature").
        # An absolute number conflates "hot because summer" with "hot because venting is
        # failing"; the delta doesn't. Validated against 19 days of real data (2026-07-19):
        # every legitimate operating hour, including nights that intentionally ran the
        # bathroom into the high 20s, stayed under +9.9C above outdoor; the 2026-07-17->18
        # incident (restricted venting, see module docstring) was already past +11.4C within
        # an hour of onset and reached +21.2C at its worst while the OLD absolute 33C cap
        # hadn't even tripped yet. 12 gives a couple degrees of margin above the observed
        # all-time-normal ceiling.
        self.bathroom_max = float(a("bathroom_backleak_c", 30.0))
        self.bathroom_delta_max = float(a("bathroom_delta_max_c", 12.0))
        # --- actuation. Fan draw is trivial (44 W even at full, measured 2026-07-15 sweep;
        # the compressor is ~500-800 W) and NO fan mode prevents the intake stall below, so
        # medium is kept purely for air distribution. Low setpoint so it doesn't idle a
        # degree early. ---
        self.cool_setpoint = float(a("cool_setpoint", 17.0))
        self.cool_fan = a("cool_fan_mode", "medium")
        # Quiet cooling (user, 2026-07-19: watching TV in bed -> cool, just less noisy):
        # while occupied, actively-cooling ticks use this fan speed instead of cool_fan.
        # Same _bed_occupied() signal as the burp quiet-gate below; slower airflow trades
        # some delivered cooling rate for noise, same trade the user is explicitly asking for.
        self.cool_fan_quiet = a("cool_fan_quiet_mode", "silent")
        self.cool_kw = float(a("cool_power_kw", 0.6))   # gentle draw, est-cost only
        # Stall-breaker: the unit regulates off its own intake sensor, which sits in its
        # cold outflow pool -- with the floor still far above target it parks itself at
        # ~300 W "idle", cooling nothing (fan mode can't fix it; measured low/high/full
        # 2026-07-15). A short fan_only burp lets the intake read true room air, after
        # which cooling restarts hard (~44 W spent, 550-800 W of real work resumes).
        self.stall_fanonly_min = int(a("stall_fanonly_min", 3))
        self.stall_burp_cooldown_min = int(a("stall_burp_cooldown_min", 15))
        self.stall_deficit_min = float(a("stall_deficit_min", 0.3))
        # Dry-finish (user, 2026-07-17: "could we use the dry mode... when humidity is high?"):
        # once the floor target is met in the evening, spend held minutes in `dry` mode when
        # the air is damp -- low fan over a cold coil pulls more water per kWh than cool mode,
        # QUIETLY (no burp roars), right before the room gets sealed. The overnight dew-point
        # climb from breathing is unavoidable; a drier start delays the clammy crossing.
        # Bounded per night; earlier-in-the-day drying is pointless (air re-exchanges).
        self.dry_from_hour = int(a("dry_finish_from_hour", 20))
        self.dry_dp = float(a("dry_finish_dp_c", 12.0))
        self.dry_max_min = float(a("dry_finish_max_min", 45))
        # Bed occupancy = quiet signal (user, 2026-07-16: "someone in bed is fine" -- presence
        # is reliable even though the 2-sensor COUNT is not): no stall-burps while either side
        # is occupied. The silence->800W roar cycle is what bothered a person trying to sleep;
        # a parked crawl is accepted until the Remove press ends the night anyway.
        self.bed_sensors = list(a("bed_occupancy_sensors",
                                  ["binary_sensor.left_bedside", "binary_sensor.right_bedside"]))
        self.dry_run = bool(a("dry_run", True))
        self.interval_min = int(a("check_interval_min", 15))
        self.min_cycle_min = int(a("min_cycle_min", 10))
        self.status_entity = a("status_entity", "sensor.smart_cooling_status")
        self.notify_target = a("notify_target", "user")
        # Grace period before the deploy watchdog speaks up: long enough to absorb a
        # normal arm-before-plugging-in gap and the cloud-session blips that already
        # self-heal in under a minute; short enough that a genuinely dead plug (2026-
        # 07-19: physical button pressed alongside the nightly unplug) doesn't eat
        # the whole afternoon before anyone notices.
        self.deploy_watchdog_min = float(a("deploy_watchdog_min", 20))
        self.state_file = a("state_file", "/conf/apps/climate/smart_cooling_state.json")

        # --- state ---
        self._last_switch: Optional[datetime] = None
        self._last_action: Optional[str] = None
        self._master_was_on: Optional[bool] = None   # one-shot AC-off on the on->off flip
        self._last_reason: Optional[str] = None
        # in-flight stall-burp window + last burp time (in-memory: a reload mid-burp just
        # means the next tick finds fan_only and sets cool again -- self-healing)
        self._burp_until: Optional[datetime] = None
        self._last_burp: Optional[datetime] = None
        # --- feasibility (user, 2026-07-15: "we might have a limit on how low the cooling
        # can feasibly get... if we keep doing 600+W in the expensive hours don't we lose
        # some of what we wanted?"). The ideal target can sit below what the unit + the
        # apartment's heat can physically deliver; without a cap the unclosable deficit
        # inflates minutes_needed until the scheduler goes time-constrained and grinds
        # through the evening price peak chasing the impossible. Detection is closed-loop
        # like everything else: sat_engaged_min minutes of wanting-to-cool with zero floor
        # progress = tonight's feasible floor. Learned across nights (EMA) so tomorrow's
        # plan never over-promises in the first place. Code defaults only -- not knobs.
        self.sat_engaged_min = float(a("sat_engaged_min", 90))    # floor sensor reports ~hourly
        self.sat_reset_rise = float(a("sat_reset_rise", 0.5))     # warmed this much above the low -> new situation
        self.feasible_min_samples = int(a("feasible_min_samples", 2))
        self._sat_min: Optional[float] = None      # session floor minimum while pursuing
        self._sat_noprog_min = 0.0                 # engaged minutes without a new minimum
        self._saturated = False
        self._last_want = False                    # was the previous eval trying to cool?
        self._last_eval_at: Optional[datetime] = None
        self._feed_last: dict = {}                 # per-kind last feed emit (see _feed_allowed)
        # Deploy watchdog (user, 2026-07-19: armed Cool night but the AC stayed
        # unreachable all afternoon - their own smart-plug button got pressed
        # alongside the nightly unplug, so "plugged in" wasn't actually powered).
        self._not_deployed_since: Optional[datetime] = None
        self._deploy_watchdog_notified = False
        # Condenser safety watchdog (found 2026-07-19, root-caused via deep-reasoner): a
        # disarm at 2026-07-17 00:19:28 correctly did its one-shot "AC off" -- then 4s
        # later something OUTSIDE this app put the AC back into cool. Disarmed = hands
        # off = we never check anything again, so nobody watched the condenser for the
        # ~9h it then ran unmonitored; bathroom hit 39.8C (+21.2C above outdoor) before
        # the next ARMED eval's guard finally caught it. See _condenser_hazard/_evaluate.
        self._safety_off_notified = False
        # Serializes _evaluate(): overlapping listen_state triggers each schedule their
        # own run_in -> create_task, so near-simultaneous triggers (confirmed 2026-07-18
        # 09:12, three at once) can run concurrently and interleave/duplicate actuation
        # (three "Lights-out"/"AC removed" stashes that morning). A lock doesn't dedupe
        # the redundant runs, just makes them safely sequential -- each one alone is
        # idempotent (repeating a toggle-off or a lightout stash is harmless).
        self._eval_lock = asyncio.Lock()
        self._was_drying = False                   # previous eval ran the dry-finish
        self._dry_date = None                      # calendar day the dry budget belongs to
        self._dry_min = 0.0                        # dry-finish minutes spent that day
        # learned warm-up indicator (+ the in-flight coast record) + learned feasible floor
        self._rise_frac = self.default_rise_frac
        self._rise_samples = 0
        self._lightout: Optional[dict] = None
        self._feasible_floor: Optional[float] = None
        self._feasible_samples = 0
        # weather-model memory: yesterday's + today's running kitchen-temperature peak
        # (Model D's thermal-mass memory; persisted, seeded from history on cold start)
        # plus a short forecast cache so the eval loop doesn't hit the service every tick.
        self._prev_kitchen_max: Optional[float] = None
        self._kitchen_max_today: Optional[float] = None
        self._kitchen_max_date: Optional[str] = None   # ISO date the running max belongs to
        self._fc_cache = None
        self._fc_cache_at: Optional[datetime] = None
        self._load_state()

        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")

        for ent in (self.enable_entity, self.price_entity,
                    self.night_ceiling_entity, self.vent_window, self.ac_removed_entity):
            self.listen_state(self._on_trigger, ent)
        # "now" is documented to mean "first call at now + interval", not immediately - found
        # 2026-07-15 chasing a stale post-reload status (every deploy left the AC blind for up to
        # interval_min minutes unless a listened entity happened to change sooner). "immediate" is
        # AppDaemon's actual keyword for "fire the first call right away".
        self.run_every(self._run_eval, "immediate", self.interval_min * 60)
        # Seed the weather-model's kitchen-peak memory from history shortly after start,
        # so a cold start / reload has prev_day_kitchen_max before the first real rollover.
        self.run_in(self._seed_kitchen_max, 5)
        self.log(f"SmartCooling v2 started (dry_run={self.dry_run}, rise_frac={self._rise_frac:.2f}, "
                 f"samples={self._rise_samples})", level="INFO")

    # ---------- triggers ----------
    def _on_trigger(self, entity, attribute, old, new, kwargs):
        self.run_in(self._run_eval, 1)

    def _run_eval(self, kwargs):
        self.create_task(self._evaluate())

    # ---------- small async helpers ----------
    async def _num(self, entity, default):
        try:
            return float(await self.get_state(entity))
        except (TypeError, ValueError):
            return default

    async def _state(self, entity):
        try:
            return await self.get_state(entity)
        except Exception:
            return None

    async def _attr(self, entity, key, default=None):
        try:
            v = await self.get_state(entity, attribute=key)
            return v if v is not None else default
        except Exception:
            return default

    @staticmethod
    def _window_open(state):
        """Vent-window read: only an explicit "off" from the contact means closed.

        unavailable/unknown/None are Zigbee dropouts, not a closed window -- the contact
        link-flapped 5x on 2026-07-16 (~70 s blips, battery full) and the old fail-closed
        read (state == "on") shut the AC mid-cheap-slot on every blip, each time costing
        the 10-min anti-short-cycle lockout on the way back. Fail-open is safe here: a
        genuinely closed window pushes the unvented condenser room far enough above
        outdoor within minutes to trip bathroom_delta_max, and that guard stops cooling
        regardless of what the contact says."""
        return state != "off"

    def _next_midnight(self, now):
        """The price-optimizer's hard search cap (user, 2026-07-15): never plan on a cheap slot
        past midnight, since bedtime itself varies and might arrive before that slot would. Always
        strictly in the future, even if `now` is already past today's midnight."""
        return datetime(now.year, now.month, now.day) + timedelta(days=1)

    def _deadline(self, now):
        """Price-search horizon for _schedule. Tonight's midnight normally (never BANK on
        slots past it -- see _next_midnight), but never less than 1h out: approaching 23:45
        the raw midnight cap left _schedule zero slots, so it shut the AC off mid-deficit
        while the user was still up (bit us 2026-07-15 23:52). Past midnight (00-06) the
        night is IN PROGRESS -- the AC still being deployed means nobody has gone to bed,
        and tomorrow's cheap midday slots are useless for tonight -- so roll a 1h
        maintenance horizon at the going (reliably cheap) price, until the AC-removed press
        or 06:00. See _reach_target for what this horizon aims AT past midnight -- not just
        the physics target anymore, since the hour is cheap and sealing is imminent."""
        if now.hour < 6:
            return now + timedelta(hours=1)
        return max(self._next_midnight(now), now + timedelta(hours=1))

    def _plan_deadline(self, now):
        """Two-tier horizon (user, 2026-07-16: "22-00 is just bonus, not the period we know
        we can cool ... we need to prepare before"). Bedtime spans 22:00-01:00, so only slots
        BEFORE 22:00 are guaranteed to exist -- anything later can be erased by the AC-removed
        press, and a top-up that starts at 22:08 is a noisy machine next to someone trying to
        sleep. So until 22:00 the load-bearing plan must fit before 22:00, even at peak prices
        (user 2026-07-15: being ready for an early bed beats the last kroner). From 22:00 the
        BONUS tier opens: the user is evidently still up, so shop the full _deadline horizon
        (midnight cap, then the overnight maintenance window) and keep improving the night.
        The 15-min floor keeps the final pre-22:00 minutes cooling on a live deficit instead
        of stranding it on a zero-slot horizon (same failure shape _deadline guards at 23:5x)."""
        if 6 <= now.hour < 22:
            guaranteed = datetime(now.year, now.month, now.day, 22, 0)
            return max(guaranteed, now + timedelta(minutes=15))
        return self._deadline(now)

    def _build_price_map(self, *arrays):
        pm = {}
        for arr in arrays:
            if not arr:
                continue
            for item in arr:
                try:
                    dt = datetime.fromisoformat(item.get("hour"))
                    pm[(dt.year, dt.month, dt.day, dt.hour)] = float(item.get("price"))
                except Exception:
                    continue
        return pm

    def _price_for(self, pm, dt, fallback):
        return pm.get((dt.year, dt.month, dt.day, dt.hour), fallback)

    # ---------- learned warm-up indicator ----------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
            self._rise_frac = float(d.get("rise_frac", self._rise_frac))
            self._rise_samples = int(d.get("rise_samples", 0))
            self._lightout = d.get("lightout")
            ff = d.get("feasible_floor")
            self._feasible_floor = float(ff) if ff is not None else None
            self._feasible_samples = int(d.get("feasible_samples", 0))
            pk = d.get("prev_kitchen_max")
            self._prev_kitchen_max = float(pk) if pk is not None else None
            km = d.get("kitchen_max_today")
            self._kitchen_max_today = float(km) if km is not None else None
            self._kitchen_max_date = d.get("kitchen_max_date")
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"rise_frac": self._rise_frac, "rise_samples": self._rise_samples,
                           "lightout": self._lightout,
                           "feasible_floor": self._feasible_floor,
                           "feasible_samples": self._feasible_samples,
                           "prev_kitchen_max": self._prev_kitchen_max,
                           "kitchen_max_today": self._kitchen_max_today,
                           "kitchen_max_date": self._kitchen_max_date}, f)
        except Exception as e:
            self.log(f"state save failed ({e}) -- continuing in-memory", level="WARNING")

    def _equilibrium(self, kitchen, mid, floor):
        """Where the sealed sleeping zone drifts overnight. Driven by the neighbour wall (kitchen)
        and the room's own warm baseline + the sleeper. Conservative: take the warmest sensible
        reading (errs deep -> safe)."""
        vals = [v for v in (kitchen, mid, floor) if v is not None]
        return (max(vals) if vals else 24.5) + self.person_offset

    # ---------- weather-driven equilibrium (verified Model D; shadow-gated) ----------
    @staticmethod
    def _clearsky_wm(hour, sunrise, sunset, peak):
        """Clear-sky half-sine irradiance (W/m2) at local `hour` (float hour-of-day), 0
        outside the [sunrise, sunset] daylight window. Pure: sunrise/sunset are float
        local hours-of-day; peak is the clear-sky amplitude (wm_clearsky_peak)."""
        if sunset <= sunrise or hour <= sunrise or hour >= sunset:
            return 0.0
        return peak * max(0.0, math.sin(math.pi * (hour - sunrise) / (sunset - sunrise)))

    def _track_kitchen_max(self, now, kitchen):
        """Running daily max of the kitchen temperature with a local-midnight rollover: on
        the first tick of a new calendar day, the peak accumulated under the previous date
        becomes prev_kitchen_max (Model D's thermal-mass memory) and today's max resets.
        Only a peak from *exactly yesterday* is promoted; a stored date older than yesterday
        (a multi-day downtime gap) drops the memory to None instead of promoting a stale peak,
        letting _seed_kitchen_max rebuild it from history. Persisted so it survives
        reloads/HA restarts. Called every eval tick."""
        today = now.strftime("%Y-%m-%d")
        if self._kitchen_max_date != today:
            yesterday = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
            if self._kitchen_max_date == yesterday:
                # Normal nightly rollover: yesterday's accumulated peak becomes the memory.
                if self._kitchen_max_today is not None:
                    self._prev_kitchen_max = self._kitchen_max_today
            else:
                # Multi-day gap (e.g. downtime): the stored peak is older than yesterday, so
                # it can't stand in for the previous day's memory. Drop it to None so
                # _seed_kitchen_max rebuilds from history (=> legacy fallback until then)
                # rather than biasing the model with a stale peak.
                self._prev_kitchen_max = None
            self._kitchen_max_date = today
            self._kitchen_max_today = kitchen if kitchen is not None else None
            self._save_state()
            return
        if kitchen is None:
            return
        if self._kitchen_max_today is None or kitchen > self._kitchen_max_today:
            self._kitchen_max_today = kitchen
            self._save_state()

    async def _seed_kitchen_max(self, kwargs):
        """Cold-start seed: derive prev_day_kitchen_max and today's running max from ~48h of
        kitchen-temperature history, so the weather model has its memory before the first
        real rollover. Best-effort -- leaves values None (=> legacy fallback) on any failure,
        and never clobbers a prev already restored from the state file."""
        try:
            now = (await self.get_now()).replace(tzinfo=None)
            tz = (await self.get_now()).tzinfo
            start = datetime(now.year, now.month, now.day) - timedelta(days=1)
            hist = await self.get_history(entity_id=self.kitchen_sensor,
                                          start_time=start, end_time=now)
            series = hist[0] if hist and isinstance(hist, list) else []
            today, yday = now.date(), (now.date() - timedelta(days=1))
            today_max = self._kitchen_max_today
            yday_max = None
            for item in series:
                v = item.get("state")
                ts = item.get("last_changed") or item.get("last_updated")
                if ts is None or v in (None, "unknown", "unavailable", ""):
                    continue
                try:
                    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    t = t.astimezone(tz).replace(tzinfo=None) if t.tzinfo else t
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                d = t.date()
                if d == today:
                    today_max = fv if today_max is None else max(today_max, fv)
                elif d == yday:
                    yday_max = fv if yday_max is None else max(yday_max, fv)
            changed = False
            if yday_max is not None and self._prev_kitchen_max is None:
                self._prev_kitchen_max = round(yday_max, 2)
                changed = True
            if today_max is not None:
                self._kitchen_max_today = round(today_max, 2)
                self._kitchen_max_date = now.strftime("%Y-%m-%d")
                changed = True
            if changed:
                self._save_state()
            self.log(f"weather-model seed: prev_kitchen_max={self._prev_kitchen_max}, "
                     f"kitchen_max_today={self._kitchen_max_today}", level="INFO")
        except Exception as e:
            self.log(f"weather-model kitchen-max seed failed ({e})", level="WARNING")

    async def _history_time_mean(self, entity, start, end):
        """Time-weighted mean of a numeric sensor between start and end (naive local).
        Returns None on empty/failed history."""
        try:
            hist = await self.get_history(entity_id=entity, start_time=start, end_time=end)
        except Exception as e:
            self.log(f"weather-model history fetch failed for {entity} ({e})", level="WARNING")
            return None
        series = hist[0] if hist and isinstance(hist, list) else []
        tz = (await self.get_now()).tzinfo
        points = []
        for item in series:
            try:
                ts = item.get("last_changed") or item.get("last_updated")
                v = item.get("state")
                if ts is None or v in (None, "unknown", "unavailable", ""):
                    continue
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                t = t.astimezone(tz).replace(tzinfo=None) if t.tzinfo else t
                points.append((t, float(v)))
            except (TypeError, ValueError, AttributeError):
                continue
        if not points:
            return None
        points.sort(key=lambda p: p[0])
        acc, total_w = 0.0, 0.0
        for i, (t, v) in enumerate(points):
            seg_start = max(t, start)
            seg_end = end if i + 1 >= len(points) else min(points[i + 1][0], end)
            w = (seg_end - seg_start).total_seconds()
            if w <= 0:
                continue
            acc += v * w
            total_w += w
        if total_w <= 0:   # all points bunched at/after `end` -> simple mean
            return sum(v for _, v in points) / len(points)
        return acc / total_w

    async def _history_max(self, entity, start, end):
        """Max of a numeric sensor's states between start and end. None on empty/failed."""
        try:
            hist = await self.get_history(entity_id=entity, start_time=start, end_time=end)
        except Exception:
            return None
        series = hist[0] if hist and isinstance(hist, list) else []
        vals = []
        for item in series:
            v = item.get("state")
            if v in (None, "unknown", "unavailable", ""):
                continue
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return max(vals) if vals else None

    async def _sun_window(self, now):
        """Local (sunrise, sunset) as float hours-of-day from sun.sun next_rising/
        next_setting. Only the time-of-day matters for the clear-sky window, so the events
        being tomorrow's is fine. Returns (None, None) on any failure."""
        try:
            tz = (await self.get_now()).tzinfo
            rise = await self._attr(self.sun_entity, "next_rising", None)
            sett = await self._attr(self.sun_entity, "next_setting", None)
            if rise is None or sett is None:
                return None, None
            rd = datetime.fromisoformat(str(rise).replace("Z", "+00:00")).astimezone(tz)
            sd = datetime.fromisoformat(str(sett).replace("Z", "+00:00")).astimezone(tz)
            return (rd.hour + rd.minute / 60.0, sd.hour + sd.minute / 60.0)
        except Exception:
            return None, None

    async def _get_forecast(self, now):
        """Hourly forecast as a list of {'dt': local-naive datetime, 'temp': float,
        'cloud': fraction|None}, cached ~30 min. Returns None if the service call fails or
        yields nothing (caller degrades to legacy / full clear-sky). Never raises, never
        stalls the eval loop (bounded wait_for)."""
        if (self._fc_cache is not None and self._fc_cache_at is not None
                and (now - self._fc_cache_at).total_seconds() < 1800):
            return self._fc_cache
        try:
            resp = await asyncio.wait_for(
                self.call_service("weather/get_forecasts",
                                  entity_id=self.weather_forecast_entity,
                                  type="hourly", return_response=True),
                timeout=12)
        except Exception as e:
            self.log(f"weather-model forecast fetch failed ({e})", level="WARNING")
            return None
        node = resp
        for key in ("result", "response", self.weather_forecast_entity, "forecast"):
            if isinstance(node, dict) and key in node:
                node = node[key]
        if not isinstance(node, list):   # dig for any forecast list in the envelope
            def find(n):
                if isinstance(n, list) and n and isinstance(n[0], dict) and "temperature" in n[0]:
                    return n
                if isinstance(n, dict):
                    for v in n.values():
                        r = find(v)
                        if r is not None:
                            return r
                return None
            node = find(resp) or []
        tz = (await self.get_now()).tzinfo
        out = []
        for item in node:
            try:
                dt = datetime.fromisoformat(str(item["datetime"]).replace("Z", "+00:00"))
                ldt = dt.astimezone(tz).replace(tzinfo=None)
                temp = float(item["temperature"])
            except (KeyError, TypeError, ValueError):
                continue
            cloud = item.get("cloud_coverage")
            try:
                cloud = float(cloud) / 100.0 if cloud is not None else None
            except (TypeError, ValueError):
                cloud = None
            out.append({"dt": ldt, "temp": temp, "cloud": cloud})
        if not out:
            return None
        self._fc_cache, self._fc_cache_at = out, now
        return out

    async def _solar_mean_today(self, now):
        """24h daily-mean solar irradiance (W/m2), assembled measured-so-far + forecast
        remainder: measured time-weighted mean from local midnight to now, plus a clear-sky
        half-sine * cloud attenuation over each remaining daylight hour (full clear-sky when
        the cloud forecast is missing = conservative/warm). None on missing solar sensor or
        failed history."""
        st = await self._state(self.solar_sensor)
        if st in (None, "unknown", "unavailable"):
            return None
        midnight = datetime(now.year, now.month, now.day)
        elapsed_h = max(0.0, (now - midnight).total_seconds() / 3600.0)
        measured_mean = await self._history_time_mean(self.solar_sensor, midnight, now)
        if measured_mean is None:
            return None
        sunrise, sunset = await self._sun_window(now)
        if sunrise is None or sunset is None:
            return None
        fc = await self._get_forecast(now)
        cloud_by_hour = {}
        if fc:
            for row in fc:
                cloud_by_hour[(row["dt"].year, row["dt"].month,
                               row["dt"].day, row["dt"].hour)] = row["cloud"]
        remaining = 0.0
        for h in range(now.hour + 1, 24):
            cs = self._clearsky_wm(h + 0.5, sunrise, sunset, self.wm_clearsky_peak)
            if cs <= 0.0:
                continue
            cf = cloud_by_hour.get((now.year, now.month, now.day, h))
            atten = (1.0 - self.wm_cloud_atten * cf) if cf is not None else 1.0
            remaining += cs * max(0.0, atten)
        return (measured_mean * elapsed_h + remaining) / 24.0

    async def _outdoor_max_today(self, now):
        """Today's outdoor peak: max(measured max-so-far via history, current reading, and
        forecast temps over the remaining hours today). None only if no source yields a
        value (=> caller degrades to legacy)."""
        vals = []
        midnight = datetime(now.year, now.month, now.day)
        measured_max = await self._history_max(self.outdoor_sensor, midnight, now)
        if measured_max is not None:
            vals.append(measured_max)
        cur = await self._num(self.outdoor_sensor, None)
        if cur is not None:
            vals.append(cur)
        fc = await self._get_forecast(now)
        if fc:
            for row in fc:
                if row["dt"].date() == now.date() and row["dt"] >= now:
                    vals.append(row["temp"])
        return max(vals) if vals else None

    async def _weather_equilibrium(self, now, kitchen, mid, floor, e_legacy):
        """Verified Model D equilibrium from weather (solar + outdoor peak + one day of
        thermal-mass memory). Returns (E, dbg). Degrades to e_legacy -- EXACTLY current
        production behaviour -- on the master kill-switch, unseeded memory, the hot-day
        forecast guard, any missing input, or any exception. Applies the one-sided safety
        floor E = max(E_weather, e_legacy - wm_nowcast_relief) so a broken model/forecast
        can never drive E materially colder than the live apartment reading implies. dbg is
        ALWAYS returned (equilibrium_weather None when it fell back) for shadow publishing.
        This method never gates on wm_shadow -- it always computes; the shadow decision (use
        E vs keep legacy driving) lives in _evaluate_locked."""
        dbg = {
            "equilibrium_weather": None,
            "equilibrium_legacy": round(e_legacy, 2),
            "solar_mean_est": None,
            "outdoor_max_est": None,
            "kitchen_max_pred": None,
            "prev_kitchen_max": (round(self._prev_kitchen_max, 2)
                                 if self._prev_kitchen_max is not None else None),
        }
        if not self.weather_model_enabled:
            return e_legacy, dbg
        try:
            if self._prev_kitchen_max is None:
                return e_legacy, dbg   # memory not seeded yet -> current behaviour
            fc = await self._get_forecast(now)
            # Hot-day guard: no forecast AND today's peak hasn't happened yet -> measured-
            # so-far under-states the coming load and there's nothing to fill the gap, so
            # fall back rather than risk the cardinal under-prediction (a too-warm room).
            if fc is None and now.hour < self.wm_peak_hour:
                return e_legacy, dbg
            solar_mean = await self._solar_mean_today(now)
            if solar_mean is None:
                return e_legacy, dbg
            outdoor_max = await self._outdoor_max_today(now)
            if outdoor_max is None:
                return e_legacy, dbg
            e_apartment = (self.wm_b0
                           + self.wm_b_solar * solar_mean
                           + self.wm_b_vent * max(0.0, outdoor_max - self.wm_vent_knee)
                           + self.wm_b_prev * self._prev_kitchen_max)
            e_weather = e_apartment + self.person_offset + self.wm_safety_margin
            E = max(e_weather, e_legacy - self.wm_nowcast_relief)
            dbg["equilibrium_weather"] = round(e_weather, 2)
            dbg["solar_mean_est"] = round(solar_mean, 1)
            dbg["outdoor_max_est"] = round(outdoor_max, 1)
            dbg["kitchen_max_pred"] = round(e_apartment, 2)
            return round(E, 2), dbg
        except Exception as e:
            self.log(f"weather-model equilibrium failed ({e}) -- using legacy", level="WARNING")
            return e_legacy, dbg

    def _calc_target(self, E, ceiling):
        """Floor target so the sleeping zone stays <= ceiling for the window. The mid wall sits
        ~zone_offset above the floor, so cap the FLOOR peak at (ceiling - zone_offset). The floor
        rises by (E - F0)*rise_frac over the window, so F0 + (E-F0)*r <= cap  ->
        F0 = (cap - E*r)/(1 - r). Clamp to [min_temp, ceiling]."""
        cap = ceiling - self.zone_offset
        r = min(0.95, max(0.05, self._rise_frac))
        if E <= cap:
            return ceiling            # room won't break the ceiling on its own -> no pre-cool
        f0 = (cap - E * r) / (1.0 - r)
        return max(self.min_temp, min(ceiling, round(f0, 2)))

    def _schedule(self, now, deadline, minutes_needed, price_at):
        """Reserve the cheapest `minutes_needed` of 15-min slots between now and deadline (midnight -
        see _next_midnight). Cool NOW if the current slot is one of them, or if there isn't time left
        to wait. Returns (cool_now, next_start, run_min, est_cost)."""
        total = int((deadline - now).total_seconds() // 900)
        if total <= 0 or minutes_needed <= 0:
            return False, None, 0, 0.0
        need = min(total, int((minutes_needed + 14.999) // 15))
        slots = [now + timedelta(minutes=15 * k) for k in range(total)]
        order = sorted(range(total), key=lambda k: price_at(slots[k]))
        chosen = sorted(order[:need])
        cool_now = (0 in chosen) or (need >= total)
        next_start = slots[chosen[0]] if chosen else None
        est = sum(self.cool_kw * 0.25 * price_at(slots[k]) for k in chosen)
        return cool_now, next_start, need * 15, round(est, 2)

    async def _learn(self, now):
        """Runs every tick (any arm/deploy state, read-only): once a stashed lights-out window
        (see _stash_lightout) finishes, learn the gap-fraction the zone closed over it."""
        floor = await self._num(self.floor_sensor, None)
        if floor is None:
            return
        lo = self._lightout
        if not lo:
            return
        try:
            ended = now >= datetime.fromisoformat(lo["end"])
        except Exception:
            self._lightout = None
            self._save_state()
            return
        if not ended:
            return
        F0, E = lo["F0"], lo["E"]
        rise, gap = floor - F0, E - F0
        self._lightout = None
        if gap > 0.5 and rise > 0:
            r_obs = max(0.05, min(0.98, rise / gap))
            n = self._rise_samples
            w = 1.0 / min(8, n + 1)   # EMA, faster while young
            self._rise_frac = (1 - w) * self._rise_frac + w * r_obs
            self._rise_samples = n + 1
            self.log(f"Learned coast {lo['date']}: floor {F0:.1f}->{floor:.1f} "
                     f"(rose {rise:.1f} of {gap:.1f} gap) r={r_obs:.2f}; "
                     f"rise_frac now {self._rise_frac:.2f} (n={self._rise_samples})", level="INFO")
        self._save_state()

    # ---------- feasibility (how low can the floor actually go) ----------
    def _track_progress(self, floor, engaged_min):
        """Feed each evaluation's floor reading + how many minutes we've been TRYING to
        cool since the previous one (0 when holding/off -- coast time is not evidence).
        Saturated = sat_engaged_min engaged minutes without a new floor minimum: the
        floor has stopped taking cold at this depth tonight."""
        if self._sat_min is None:
            self._sat_min = floor
            self._sat_noprog_min = 0.0
            return False
        if floor < self._sat_min - 0.05:          # real progress -> reset the clock
            self._sat_min = floor
            self._sat_noprog_min = 0.0
            self._saturated = False
            return False
        if floor > self._sat_min + self.sat_reset_rise:
            # warmed well above the low point (evening drift, door opened) -> new
            # situation; topping back DOWN to the known-feasible depth is worthwhile
            # and the scheduler will place it in the cheapest remaining slots.
            self._sat_min = floor
            self._sat_noprog_min = 0.0
            self._saturated = False
            return False
        self._sat_noprog_min += max(0.0, engaged_min)
        if not self._saturated and self._sat_noprog_min >= self.sat_engaged_min:
            self._saturated = True
            self._learn_feasible(self._sat_min)
        return self._saturated

    def _reach_target(self, now, target, saturated):
        """What we actually pursue this tick. `target` is the physics-ideal minimum for
        tonight's ceiling; this widens or tightens it against real-world limits, checked
        in priority order:
          1. Tonight's REAL saturation (measured, can't argue with it) -- never chase
             past a floor we've already proven won't move.
          2. Post-midnight bonus (user, 2026-07-17: "keep cooling if it make the room more
             comfortable after 00:00, energy is always cheaper then"). Sealing is
             imminent at that hour, so there's no daytime decay to out-leak the extra
             depth, and 00-06 is reliably one of the cheapest windows of the day (see the
             July price sweep) -- so aim for the hardware floor, not just the minimum
             needed. The historical feasible-floor cap (next) is skipped here on purpose:
             trying is nearly free at these prices, and that's exactly how a milder night
             re-teaches the learner a deeper number.
          3. The historical feasible-floor cap -- elsewhere (daytime/evening), avoid
             paying peak prices chasing depth history says won't hold."""
        if saturated and self._sat_min is not None:
            return max(target, self._sat_min)
        if now.hour < 6:
            return self.min_temp
        if self._feasible_floor is not None and self._feasible_samples >= self.feasible_min_samples:
            return max(target, self._feasible_floor - 0.3)
        return target

    def _learn_feasible(self, floor_min):
        """EMA the observed can't-go-lower floor across nights, so future plans stop
        promising (and pricing) depth the unit can't deliver."""
        n = self._feasible_samples
        w = 1.0 / min(6, n + 1)
        self._feasible_floor = round(
            floor_min if self._feasible_floor is None
            else (1 - w) * self._feasible_floor + w * floor_min, 2)
        self._feasible_samples = n + 1
        self._save_state()
        self.log(f"Feasible floor tonight: {floor_min:.1f}C after {self._sat_noprog_min:.0f} "
                 f"engaged min without progress; learned limit now {self._feasible_floor:.1f}C "
                 f"(n={self._feasible_samples})", level="INFO")

    async def _maybe_dry(self, now, floor):
        """Whether a held (at-target) evening eval should run the dry-finish instead of
        sitting off. All gates must pass: evening hour (moisture removed earlier just
        re-exchanges before bed), damp air (dew point at/over the threshold), and tonight's
        bounded budget. Returns (dry_now, reason)."""
        if now.hour < self.dry_from_hour:
            return False, None
        if now.date() != self._dry_date:
            self._dry_date = now.date()
            self._dry_min = 0.0
        if self._dry_min >= self.dry_max_min:
            return False, None
        dp = await self._attr(self.comfort_entity, "dew_point", None)
        try:
            dp = float(dp)
        except (TypeError, ValueError):
            return False, None
        if dp < self.dry_dp:
            return False, None
        left = self.dry_max_min - self._dry_min
        return True, (f"Cooling done (floor {floor:.1f}C) -- drying the damp air before the "
                      f"night: dew point {dp:.1f}C, dry mode (quiet, low fan) up to "
                      f"~{left:.0f} more min tonight")

    def _stash_lightout(self, floor, E, now):
        """Record the true lights-out baseline (F0, E) the moment the user confirms AC removed -
        replaces the old bedtime-time-window guess (user, 2026-07-15: a fixed clock time is
        sometimes hours off from when they actually go to bed, which corrupted the learned rise_frac
        by measuring the coast from the wrong starting point). Overwrites any stale in-flight
        record - the latest press is always the truest lights-out moment."""
        today = now.strftime("%Y-%m-%d")
        end = now + timedelta(hours=self.sleep_hours)
        self._lightout = {"date": today, "F0": round(floor, 2), "E": round(E, 2),
                          "end": end.isoformat()}
        self._save_state()
        self.log(f"Lights-out {today}: floor {floor:.1f}C, equilibrium {E:.1f}C "
                 f"-> learning the rise at {end.strftime('%H:%M')}", level="INFO")

    async def _check_deploy_watchdog(self, now, master_on, deployed):
        """Notify once if Cool night is armed but the AC stays unreachable past the
        grace period - most likely the physical plug/switch, not a code problem.
        The streak (and the one-shot) resets the moment it deploys or gets
        disarmed, so a later recurrence can notify again."""
        if not master_on or deployed:
            self._not_deployed_since = None
            self._deploy_watchdog_notified = False
            return
        if self._not_deployed_since is None:
            self._not_deployed_since = now
            return
        stuck_min = (now - self._not_deployed_since).total_seconds() / 60.0
        if stuck_min >= self.deploy_watchdog_min and not self._deploy_watchdog_notified:
            self._deploy_watchdog_notified = True
            await self._notify(
                f"Cool night is on but the AC hasn't been reachable for "
                f"~{stuck_min:.0f} min -- check it's actually got power "
                f"(plug/switch), not just plugged in.")

    @staticmethod
    def _venting_impaired(bath, outdoor, delta_max):
        """Is the condenser room running hot enough ABOVE OUTDOOR to mean venting isn't
        keeping up -- regardless of absolute temperature? (User, 2026-07-19: "it is all
        about how warm it is compared to the outside temperature.") An absolute number
        conflates "hot because summer" with "hot because trapped"; the delta doesn't --
        see bathroom_delta_max's init comment for the 19-day validation. Fails closed on
        either missing reading, same as the old absolute check failed closed on `bath`."""
        return bath is not None and outdoor is not None and (bath - outdoor) >= delta_max

    @staticmethod
    def _condenser_hazard(deployed, climate_state, bath, outdoor, delta_max):
        """Arm-independent: should we force the AC off right now, regardless of whether
        Cool night is armed? Only true when the unit is genuinely deployed, in a mode
        that can actually be running the compressor (not off/unavailable/unknown), AND
        venting is impaired per _venting_impaired."""
        return (deployed and climate_state not in (None, "off", "unavailable", "unknown")
                and SmartCooling._venting_impaired(bath, outdoor, delta_max))

    # ---------- main ----------
    async def _evaluate(self):
        async with self._eval_lock:
            await self._evaluate_locked()

    async def _evaluate_locked(self):
        now = (await self.get_now()).replace(tzinfo=None)
        await self._learn(now)   # read-only; runs regardless of arm/deploy

        master_on = (await self._state(self.enable_entity)) == "on"
        climate_state = await self._state(self.climate_entity)
        deployed = climate_state not in (None, "unavailable", "unknown")
        bath = await self._num(self.bathroom_sensor, None)
        outdoor = await self._num(self.outdoor_sensor, None)
        await self._check_deploy_watchdog(now, master_on, deployed)

        if not master_on:
            self._mark_eval(now, False)
            if self._condenser_hazard(deployed, climate_state, bath, outdoor, self.bathroom_delta_max):
                # "OFF = HANDS OFF" means we don't plan/optimize while disarmed, NOT
                # that we ignore a physical hazard -- see the state-init comment for
                # the incident that proved the gap. Only ever forces the hard cap,
                # never routine planning, so this can't fight a genuine manual session.
                delta = bath - outdoor
                await self._ensure_off(
                    "off",
                    f"SAFETY: condenser room {bath:.1f}C is {delta:.1f}C above outdoor "
                    f"({outdoor:.1f}C) -- venting isn't keeping up, forcing the AC off "
                    f"(disarmed, but this isn't optional)",
                    {"deployed": deployed, "bathroom": round(bath, 1)})
                if not self._safety_off_notified:
                    self._safety_off_notified = True
                    await self._notify(
                        f"Safety: the bedroom AC was running with the bathroom "
                        f"{delta:.1f}C above outdoor ({bath:.1f}C vs {outdoor:.1f}C) "
                        f"while Cool night was off -- forced it off. Check what turned "
                        f"it on.")
                self._master_was_on = False
                return
            self._safety_off_notified = False
            # OFF = HANDS OFF. Turn the AC off ONCE on the on->off flip, then never command it again.
            if self._master_was_on:
                await self._ensure_off("off", "Disarmed -- AC turned off, now hands-off", {"deployed": deployed})
            else:
                await self._publish("off", "Disarmed -- hands off (manual AC control)", {"deployed": deployed})
            self._master_was_on = False
            return
        self._master_was_on = True
        if not deployed:
            self._mark_eval(now, False)
            await self._publish("unit_stored", "AC not deployed (climate unavailable)", {})
            return

        floor = await self._num(self.floor_sensor, None)
        if floor is None:
            self._mark_eval(now, False)
            await self._publish("no_data", "Missing bedroom floor temperature", {})
            return
        mid = await self._num(self.mid_sensor, floor)
        ceil_s = await self._num(self.ceiling_sensor, None)
        ac_s = await self._num(self.ac_sensor, None)
        kitchen = await self._num(self.kitchen_sensor, None)
        self._track_kitchen_max(now, kitchen)   # running daily max + midnight rollover

        ceiling_base = await self._num(self.night_ceiling_entity, self.default_ceiling)
        # Comfort layer may lower the ceiling on humid/two-sleeper nights. Bounded:
        # at most comfort_max_reduction below the knob, never below min_temp, never
        # raised above the knob; any comfort-layer problem falls back to the knob.
        ceiling = ceiling_base
        try:
            ce = await self.get_state(self.comfort_entity, attribute="ceiling_effective")
            if ce is not None:
                ceiling = min(ceiling_base,
                              max(float(ce), ceiling_base - self.comfort_max_reduction, self.min_temp))
        except (TypeError, ValueError):
            pass
        deadline = self._plan_deadline(now)

        pm = self._build_price_map(
            await self._attr(self.price_entity, "raw_today", []),
            await self._attr(self.price_entity, "raw_tomorrow", []),
        )
        price_now = self._price_for(pm, now, await self._num(self.price_entity, 1.7))
        price_at = lambda dt: self._price_for(pm, dt, price_now)
        window_open = self._window_open(await self._state(self.vent_window))

        zone = round((floor + mid) / 2.0, 1)            # the floor-to-mid sleeping zone
        # Legacy proxy stays the fallback + the relief-floor anchor; the weather model is
        # computed EVERY tick but only DRIVES actuation once it's enabled and out of shadow.
        # While wm_shadow is true, E == e_legacy exactly (zero actuation change) and the
        # weather value rides along on the status entity for predicted-vs-actual validation.
        e_legacy = self._equilibrium(kitchen, mid, floor)
        e_active, wm_dbg = await self._weather_equilibrium(now, kitchen, mid, floor, e_legacy)
        E = e_active if (self.weather_model_enabled and not self.wm_shadow) else e_legacy
        target = self._calc_target(E, ceiling)
        deficit = floor - target
        floor_limited = target <= self.min_temp + 0.05  # hot night: cooling as deep as the unit allows

        # `target` stays the IDEAL for display/learning; `reach_target` is what we actually
        # pursue -- widened past midnight for the cheap-power bonus, tightened by the
        # feasibility cap when the floor has proven it won't go lower (see _reach_target).
        engaged = 0.0
        if self._last_want and self._last_eval_at is not None:
            engaged = min((now - self._last_eval_at).total_seconds() / 60.0,
                          self.interval_min * 1.5)
        saturated = self._track_progress(floor, engaged)
        reach_target = self._reach_target(now, target, saturated)
        reach_deficit = floor - reach_target
        minutes_needed = max(0.0, reach_deficit) / self.floor_cool_cph * 60.0

        # user says they're removing the AC now -> this IS lights-out: stash the coast baseline,
        # graceful compressor stop (they unplug right after -- better than yanking power
        # mid-run), then reset the toggle so it's a one-shot trigger.
        if (await self._state(self.ac_removed_entity)) == "on":
            self._mark_eval(now, False)
            self._stash_lightout(floor, E, now)
            await self._ensure_off(
                "done_for_tonight",
                "AC removed -- sealing the bedroom for the night.",
                self._attrs(floor, mid, zone, ceil_s, ac_s, bath, kitchen, E, target, deficit,
                            ceiling, price_now, window_open, 0, None, 0.0, floor_limited,
                            ceiling_base, wm_dbg=wm_dbg),
            )
            try:
                await self.call_service("input_boolean/turn_off", entity_id=self.ac_removed_entity)
            except Exception as e:
                self.log(f"failed to reset ac_removed toggle: {e}", level="WARNING")
            # The press also DISARMS the master (user design 2026-07-17): the physical unplug
            # is the real seal, so if the unit stays plugged a still-armed planner would
            # resume cooling at the next cheap slot beside the sleeping user (nearly happened
            # 2026-07-16 23:00). Disarm = visible on the card, no hidden seal state, and
            # re-arming is already the user's morning ritual.
            try:
                self._master_was_on = False   # skip the disarm listener's redundant one-shot off
                await self.call_service("input_boolean/turn_off", entity_id=self.enable_entity)
                self.log("AC removed -> disarmed for the night (arm again when redeploying)",
                         level="INFO")
            except Exception as e:
                self.log(f"failed to disarm after AC-removed: {e}", level="WARNING")
            return

        cool_now, next_start, run_min, est_cost = self._schedule(now, deadline, minutes_needed, price_at)
        slots_left = int((deadline - now).total_seconds() // 900)
        time_constrained = run_min >= max(1, slots_left) * 15
        # Bathroom heat is the condenser's own dump, not a bedroom threat: the user seals the
        # bathroom door (2026-07-16), so back-leak is ~nil and a warm condenser room only costs
        # efficiency (~2-3%/C). That penalty is far smaller than the cheap-vs-peak price spread,
        # so we push through warm-bathroom slots while power is cheap and let the venting happen
        # in hours we'd hold anyway. Only impaired venting stops cooling -- judged by how far
        # ABOVE OUTDOOR the bathroom sits, not an absolute number (see bathroom_delta_max).
        backleak_hard = self._venting_impaired(bath, outdoor, self.bathroom_delta_max)
        bath_warm = bath is not None and bath >= self.bathroom_max

        # decision
        if floor <= self.min_temp:
            want, reason = False, f"Floor at min ({floor:.1f}<= {self.min_temp:.1f}) -- holding"
        elif reach_deficit <= 0.05:
            # Single source of truth for "are we done" -- `target` alone would stop the
            # post-midnight bonus (below) the moment the SHALLOW physics minimum was hit,
            # never reaching the deeper reach_target it's actually chasing.
            if reach_target > target + 0.05:
                lim = self._sat_min if saturated else self._feasible_floor
                want, reason = False, (f"As cold as it feasibly gets: floor {floor:.1f}C, ideal "
                                       f"{target:.1f}C, but the floor stops taking cold around "
                                       f"{lim:.1f}C -- holding rather than paying for cooling it "
                                       f"won't absorb")
            elif reach_target < target - 0.05:
                want, reason = False, (f"Banked past the minimum: floor {floor:.1f}C, cheap "
                                       f"post-midnight power got it colder than the {target:.1f}C "
                                       f"actually needed -- holding")
            else:
                want, reason = False, f"On track: floor {floor:.1f}C <= target {target:.1f}C (zone {zone:.1f}, cap {ceiling:.0f})"
        elif not window_open:
            want, reason = False, "Bathroom window closed -- open it so the condenser can vent"
        elif backleak_hard:
            want, reason = False, (f"Bathroom {bath:.1f}C is {bath - outdoor:.1f}C above outdoor "
                                   f"({outdoor:.1f}C) -- venting isn't keeping up, easing off "
                                   f"before the condenser derates")
        elif cool_now:
            want = True
            reason = (f"Pre-cool floor {floor:.1f}->{reach_target:.1f}C (keep zone <= {ceiling:.0f} for "
                      f"{self.sleep_hours:.0f}h): ~{run_min} min in the cheapest hours, ~{est_cost:.1f} kr, "
                      f"price {price_now:.2f}"
                      + ("  [floor-limited: hottest it can do]" if floor_limited else "")
                      + (f"  [capped by feasible ~{reach_target:.1f}C, ideal {target:.1f}C]"
                         if reach_target > target + 0.05 else "")
                      + (f"  [bonus: cheap post-midnight power, past the {target:.1f}C minimum]"
                         if reach_target < target - 0.05 else "")
                      + (f"  [condenser room {bath:.1f}C -- pushing through the cheap slot]"
                         if bath_warm else ""))
        else:
            nx = next_start.strftime("%H:%M") if next_start else "later"
            want, reason = False, (f"Hold for cheaper power: need ~{run_min} min, start ~{nx} "
                                   f"(floor {floor:.1f}->{reach_target:.1f}C)")

        # Dry-finish bookkeeping + gate: only the at-target holds qualify (never instead of
        # needed cooling, never with the vent window shut or the condenser room at the hard
        # cap -- dry mode still dumps compressor heat into the bathroom).
        if self._was_drying and self._last_eval_at is not None:
            self._dry_min += min((now - self._last_eval_at).total_seconds() / 60.0,
                                 self.interval_min * 1.5)
        self._was_drying = False
        dry_now = False
        if not want and reach_deficit <= 0.05 and window_open and not backleak_hard:
            dry_now, dry_reason = await self._maybe_dry(now, floor)
            if dry_now:
                reason = dry_reason

        self._mark_eval(now, want)
        attrs = self._attrs(floor, mid, zone, ceil_s, ac_s, bath, kitchen, E, target, deficit,
                            ceiling, price_now, window_open, run_min, next_start, est_cost, floor_limited,
                            ceiling_base, reach_target=reach_target, wm_dbg=wm_dbg)

        if reason != self._last_reason:
            self.log(f"PLAN {reason}", level="INFO")
            self._last_reason = reason

        if want:
            await self._apply_cool(reason, "cooling_dryrun" if self.dry_run else "cooling",
                                   attrs, reach_deficit, now)
        elif dry_now:
            self._was_drying = True
            await self._apply_dry(reason, attrs)
        else:
            await self._ensure_off("waiting" if reach_deficit > 0.05 else "idle", reason, attrs)

    def _mark_eval(self, now, want):
        """Bookkeeping for the feasibility tracker: engaged time only accrues between
        evaluations where we actually wanted to cool."""
        self._last_eval_at = now
        self._last_want = want

    def _attrs(self, floor, mid, zone, ceil_s, ac_s, bath, kitchen, E, target, deficit,
               ceiling, price_now, window_open, run_min, next_start, est_cost, floor_limited,
               ceiling_base, reach_target=None, wm_dbg=None):
        def r1(v):
            return round(v, 1) if v is not None else None
        out = {
            "floor": r1(floor), "mid_wall": r1(mid), "sleeping_zone": zone,
            "ceiling_delivery": r1(ceil_s), "ac_output": r1(ac_s),
            "bathroom": r1(bath), "kitchen": r1(kitchen),
            "equilibrium_est": r1(E), "floor_target": r1(target), "deficit": round(max(0.0, deficit), 1),
            "floor_target_feasible": r1(reach_target if reach_target is not None else target),
            "saturated": self._saturated, "feasible_floor": r1(self._feasible_floor),
            "floor_low_tonight": r1(self._sat_min),
            "floor_limited": floor_limited, "night_ceiling": r1(ceiling),
            "ceiling_base": r1(ceiling_base), "ceiling_source": ("comfort layer" if ceiling < ceiling_base else "knob"),
            "min_temp": r1(self.min_temp),
            "rise_frac": round(self._rise_frac, 2), "rise_samples": self._rise_samples,
            "price_now": round(price_now, 2), "window_open": window_open,
            "minutes_needed": run_min, "next_start": next_start.strftime("%H:%M") if next_start else None,
            "est_cost_kr": est_cost, "dry_run": self.dry_run,
            "last_burp": self._last_burp.strftime("%H:%M") if self._last_burp else None,
            "dry_min_tonight": round(self._dry_min),
        }
        # Weather-model shadow attributes (equilibrium_weather/legacy, solar/outdoor/kitchen
        # estimates) for predicted-vs-actual comparison on the status entity. wm_shadow is
        # published as a STRING to survive AppDaemon's False/0/None attribute-drop (see the
        # _publish comment / window_open pattern) -- a raw False would vanish from the entity.
        if wm_dbg:
            out.update(wm_dbg)
        out["wm_shadow"] = "true" if self.wm_shadow else "false"
        return out

    # ---------- stall-breaker ----------
    def _should_burp(self, hvac_action, cur_mode, deficit, now):
        """True when the unit has parked itself: reports idle while in cool mode with real
        floor deficit left. Cooldown keeps burps a compressor-friendly distance apart."""
        if hvac_action != "idle" or cur_mode != "cool":
            return False
        if deficit < self.stall_deficit_min:
            return False
        if self._burp_until is not None and now < self._burp_until:
            return False
        if self._last_burp is not None and \
                (now - self._last_burp).total_seconds() < self.stall_burp_cooldown_min * 60:
            return False
        return True

    async def _start_burp(self, deficit, now):
        try:
            await self.call_service("climate/set_hvac_mode",
                                    entity_id=self.climate_entity, hvac_mode="fan_only")
        except Exception as e:
            self.log(f"stall-burp failed to enter fan_only: {e}", level="WARNING")
            return
        self._burp_until = now + timedelta(minutes=self.stall_fanonly_min)
        self._last_burp = now
        self.log(f"Stall-burp: unit idling with {deficit:.1f}C floor deficit -- fan-only "
                 f"{self.stall_fanonly_min} min so the intake reads room air, then cool again",
                 level="INFO")
        self.run_in(self._end_burp, self.stall_fanonly_min * 60)

    # Feed etiquette (user 2026-07-16: "very chatty"): the activity feed is a 40-entry house
    # HISTORY, not a status card. The AC's minute-to-minute breathing -- burps, cheap-slot
    # holds/resumes, target drift -- lives on the SmartCooling card; only session-level facts
    # (first cool-on of the night, sealed for the night, disarm) and actionable blockers
    # (window closed, condenser hard cap) get a feed entry, each rate-limited so a flapping
    # bathroom window or a stop-start evening cannot flood the feed.
    FEED_COOLDOWN_MIN = {"cool_on": 240, "off_window": 60, "off_hardcap": 120,
                         "disarm": 0, "done": 0}

    def _feed_kind_for_off(self, reason):
        """Which feed entry (if any) an AC-off deserves. None = card-only, no feed entry --
        notably every routine 'Hold for cheaper power' / 'On track' breather."""
        if reason.startswith("Disarmed"):
            return "disarm"
        if reason.startswith("AC removed"):
            return "done"
        if reason.startswith("Bathroom window closed"):
            return "off_window"
        if reason.startswith("SAFETY") or "venting isn't keeping up" in reason:
            return "off_hardcap"
        return None

    def _feed_allowed(self, kind, now):
        """Gate + stamp: one entry per kind per cooldown window (in-memory; a reload's worth
        of duplicate risk is fine for an activity feed)."""
        if kind is None:
            return False
        cooldown = self.FEED_COOLDOWN_MIN.get(kind, 0)
        last = self._feed_last.get(kind)
        if last is not None and cooldown > 0 and (now - last).total_seconds() < cooldown * 60:
            return False
        self._feed_last[kind] = now
        return True

    async def _report_house_event(self, kind, cause, effect, now):
        """Explain a session-level AC fact to the dashboard's Home activity feed (admin
        audience - Mikkel's bedroom AC). Kind-gated + rate-limited via _feed_allowed."""
        if not self._feed_allowed(kind, now):
            return
        try:
            await self.fire_event(
                "house_events_report",
                cause=cause,
                effect=effect,
                icon="mdi:snowflake-thermometer",
                audience="admin",
            )
        except Exception:
            pass

    def _end_burp(self, kwargs):
        self.create_task(self._end_burp_async())

    async def _end_burp_async(self):
        self._burp_until = None
        if (await self._state(self.enable_entity)) != "on":
            return   # disarmed mid-burp -- hands off
        if (await self._state(self.climate_entity)) != "fan_only":
            return   # someone/something else changed mode -- don't fight it
        try:
            await self.call_service("climate/set_hvac_mode",
                                    entity_id=self.climate_entity, hvac_mode="cool")
            await self.call_service("climate/set_temperature",
                                    entity_id=self.climate_entity, temperature=self.cool_setpoint)
            self.log("Stall-burp done -- cooling resumed", level="INFO")
        except Exception as e:
            self.log(f"stall-burp failed to resume cool: {e}", level="ERROR")

    # ---------- actuation (gentle; respects dry_run + anti-short-cycle) ----------
    @staticmethod
    def _cooling_fan(cool_fan, cool_fan_quiet, occupied):
        """Fan speed for an actively-cooling tick: the quiet speed while someone's in
        bed (2026-07-19 -- "watching TV in bed -> cool, just less noisy"), the
        configured cool_fan_mode otherwise."""
        return cool_fan_quiet if occupied else cool_fan

    async def _apply_cool(self, reason, status, attrs, deficit, now):
        cur_mode = await self._state(self.climate_entity)
        occupied = await self._bed_occupied()
        fan = self._cooling_fan(self.cool_fan, self.cool_fan_quiet, occupied)
        if not self.dry_run:
            if self._burp_until is not None and now < self._burp_until:
                await self._publish("burping", "Stall-burp in progress -- fan-only so the "
                                    "intake reads room air, cooling resumes in a moment", attrs)
                return
            action = await self._attr(self.climate_entity, "hvac_action", None)
            if self._should_burp(action, cur_mode, deficit, now):
                # Quiet gate (user, 2026-07-16): the burp's silence->800W restart is what
                # bothers a person in bed. Someone on either bedside -> skip the burp and
                # accept the parked crawl; the Remove press ends the night soon anyway.
                if occupied:
                    await self._publish(status, reason + "  [in bed -- skipping the noisy "
                                        "compressor wake-up]", attrs)
                    return
                await self._start_burp(deficit, now)
                await self._publish("burping", f"Stall-burp: idling with {deficit:.1f}C to go "
                                    f"-- fan-only {self.stall_fanonly_min} min to wake the "
                                    f"compressor", attrs)
                return
        if self.dry_run:
            await self._publish(status, reason, attrs)
            self.log(f"DRY-RUN would COOL ({self.cool_setpoint}C/{fan}): {reason}")
            return
        need_mode = cur_mode != "cool"
        if need_mode and not self._can_switch(True):
            # Deferred by anti-short-cycle: say so instead of claiming "cooling" while the
            # unit sits off (user saw exactly that 2026-07-16 12:14 and reported it as a bug).
            wait_min = self.min_cycle_min
            if self._last_switch is not None:
                wait_min = max(0.0, self.min_cycle_min
                               - (datetime.now() - self._last_switch).total_seconds() / 60.0)
            await self._publish("waiting", f"Starting in ~{wait_min:.0f} min "
                                f"(compressor rest after the last stop) -- then: {reason}", attrs)
            return
        await self._publish(status, reason, attrs)
        try:
            if need_mode:
                await self.call_service("climate/set_hvac_mode", entity_id=self.climate_entity, hvac_mode="cool")
                self._mark_switch("cool")
                self.log(f"COOL on ({self.cool_setpoint}C/{fan}): {reason}"
                         + ("  [quiet: in bed]" if occupied else ""), level="INFO")
                # reason is the planner's own explanation of WHY (cheap hour, deadline, deficit...)
                await self._report_house_event(
                    "cool_on", reason,
                    f"AC cooling the bedroom to {self.cool_setpoint:g}C", now)
            cur_temp = await self._attr(self.climate_entity, "temperature", None)
            if cur_temp is None or abs(float(cur_temp) - self.cool_setpoint) > 0.1:
                await self.call_service("climate/set_temperature", entity_id=self.climate_entity, temperature=self.cool_setpoint)
            cur_fan = await self._attr(self.climate_entity, "fan_mode", None)
            if cur_fan != fan:
                try:
                    await self.call_service("climate/set_fan_mode", entity_id=self.climate_entity, fan_mode=fan)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Failed to start cooling: {e}", level="ERROR")

    async def _apply_dry(self, reason, attrs):
        """Run the evening dry-finish: hvac dry mode (firmware runs its own low fan +
        gentle compressor cycling). Entry/exit respect the same anti-short-cycle courtesy
        as cool/off; the decision chain exits dry naturally (deficit reopens -> cool,
        gates fail -> _ensure_off, Remove press -> sealed)."""
        await self._publish("drying", reason, attrs)
        if self.dry_run:
            self.log(f"DRY-RUN would run dry mode: {reason}")
            return
        cur_mode = await self._state(self.climate_entity)
        if cur_mode == "dry":
            return
        if not self._can_switch(True):
            return
        try:
            await self.call_service("climate/set_hvac_mode",
                                    entity_id=self.climate_entity, hvac_mode="dry")
            self._mark_switch("dry")
            self.log(f"DRY on: {reason}", level="INFO")
        except Exception as e:
            self.log(f"Failed to start dry mode: {e}", level="ERROR")

    async def _bed_occupied(self):
        """Someone is in bed (either side -- presence is reliable, the count is not)."""
        for s in self.bed_sensors:
            if (await self._state(s)) == "on":
                return True
        return False

    async def _ensure_off(self, status, reason, attrs):
        self._burp_until = None   # plan flipped to off mid-burp: the burp is moot
        await self._publish(status, reason, attrs)
        cur_mode = await self._state(self.climate_entity)
        already_off = cur_mode in (None, "off", "unavailable", "unknown")
        if self.dry_run:
            if not already_off:
                self.log(f"DRY-RUN would turn AC OFF: {reason}")
            return
        if already_off or not self._can_switch(False):
            return
        try:
            await self.call_service("climate/set_hvac_mode", entity_id=self.climate_entity, hvac_mode="off")
            self._mark_switch("off")
            self.log(f"AC off: {reason}", level="INFO")
            now = (await self.get_now()).replace(tzinfo=None)
            await self._report_house_event(self._feed_kind_for_off(reason), reason,
                                           "AC turned off", now)
        except Exception as e:
            self.log(f"Failed to turn off: {e}", level="ERROR")

    def _can_switch(self, want_on):
        action = "cool" if want_on else "off"
        if self._last_switch is not None and self._last_action != action:
            mins = (datetime.now() - self._last_switch).total_seconds() / 60.0
            if mins < self.min_cycle_min:
                self.log(f"Anti-short-cycle: {mins:.1f}<{self.min_cycle_min} min, defer {action}")
                return False
        return True

    def _mark_switch(self, action):
        self._last_switch = datetime.now()
        self._last_action = action

    # ---------- notify ----------
    async def _notify(self, message):
        if not self.mobile_notifier:
            return
        try:
            await self.mobile_notifier.notify(title="Smart cooling", message=message, target=self.notify_target)
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")

    # ---------- publish ----------
    async def _publish(self, status, reason, attrs):
        a = dict(attrs or {})
        a["reason"] = reason
        a["friendly_name"] = "Smart cooling status"
        a["icon"] = "mdi:snowflake-thermometer"
        try:
            # AppDaemon 4.5.13 bug, not ours: every set_state() HTTP publish runs through
            # appdaemon.utils.clean_http_kwargs -> remove_literals(val, (None, False)), which
            # deletes any attribute key whose value equals False (or 0, since 0 == False)
            # before the /api/states POST body is built. True survives only because it's
            # separately rewritten to the string "true" first -- so a present "boolean"
            # attribute is actually that string, never a JSON bool; a MISSING key means false.
            # dry_run/floor_limited vanish from this entity whenever they're False. No supported
            # AppDaemon API bypasses this and a value-mangling wrapper would be a hack, so this
            # is left as a known framework limitation -- do not chase it here.
            # Fixed upstream by PR #2594 (merged to AppDaemon dev 2026-05-13): POST bodies now go
            # raw, query-param cleaning got its own identity-checking helper. No release carries
            # it yet (latest 4.5.13) -- these comments self-obsolete on the first upgrade past that.
            await self.set_state(self.status_entity, state=status, attributes=a, replace=True)
        except Exception as e:
            self.log(f"publish failed: {e}", level="WARNING")
