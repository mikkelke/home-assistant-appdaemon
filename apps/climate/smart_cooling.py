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

Session-cost metering (2026-07-21, validated with meter data; cost MODEL rebuilt 2026-07-29):
the sleep plan's AC estimate was 4-8x too LOW -- it priced one deficit-closing run (~1.5 kWh)
at the single cheapest 15-min slot (~0.7 kr), while the Shelly plug metered a real session's
3.5 kWh / ~4.2 kr spot (user's full tariff ~6 kr). A real session re-cools all night (re-warm
top-ups, stall-burps, the parked ~300W crawl, the deliberate post-midnight cheap-power chase),
not just the one run the old formula priced. Fix: meter reality and learn from it.
_track_session_cost accumulates every tick from ac_energy_entity (the Shelly cumulative kWh
counter -- the only reliable AC energy meter in this home); _finalize_session (AC-removed
press, or a disarmed+undeployed fallback) freezes the session totals.

The first cut of this (2026-07-21) EMA'd the session's flat kr cost into night_cost_ema, which
then REPLACED plan_sleep's theoretical estimate outright. That went stale fast: 3 finalized
0.00 kWh sessions (Cool night armed+deployed but the compressor never actually ran that night)
dragged night_cost_ema down to 0.36 kr while 15 metered days (2026-07-10..29) actually averaged
4.50 kr. The 2026-07-29 rebuild replaces it with a SCALING model instead of a flat number:
kwh_per_deg (kWh spent per degree C of pre-cool deficit closed), EMA'd from
session_kwh / deficit0 -- the deficit the sleep plan had published the moment the session
opened -- and seeded at kwh_per_deg_default from that same 15-day window. session_min_kwh
excludes trivial/no-op sessions from EVERY learned value (they carry no signal about what
cooling costs, only noise), and pricing itself is now deficit-sized: _publish_sleep_plan blends
the price over the k = ceil(minutes_needed / 15) slots the job will actually occupy instead of
a fixed est_price_slots count (real blended price paid across those 15 days was
1.39-1.52 kr/kWh vs the ~0.5 kr/kWh the old single-cheapest-slot pricing produced).
night_cost_ema/last_session_cost etc. are still learned and published (the live/display
metering readout is still correct) -- they just no longer drive the estimate. None of this
touches the armed actuation chain above; it is display/estimation only.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import asyncio
from datetime import datetime, timedelta
import json
import math
from typing import Optional

import climate_model as cm  # pure math (zero appdaemon imports; import-safe from app + tests)


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
        # Humidity-aware ceiling. comfort_entity is still read by the dry-finish gate
        # (_maybe_dry reads its RAW dew_point measurement -- outside the control loop). The
        # actuation ceiling is now computed LOCALLY (see _effective_ceiling) via the shared
        # climate_model comfort fns, so smart_cooling no longer READS ceiling_effective back
        # from that sensor (that read + bedroom_comfort's state-file rise_frac read were the
        # circular dependency between the two apps). These comfort_* knobs MUST MIRROR
        # bedroom_comfort.yaml so the locally-computed ceiling is the same number the comfort
        # sensor publishes -> byte-identical target math.
        self.comfort_entity = a("comfort_entity", "sensor.bedroom_comfort")
        self.comfort_max_reduction = float(a("comfort_max_reduction", 1.5))
        self.comfort_temp_entity = a("comfort_temp_entity", "sensor.bedroom_median_temperature")
        self.comfort_rh_entity = a("comfort_rh_entity", "sensor.bedroom_humidity")
        self.comfort_persons = list(a("comfort_persons", ["person.mikkel"]))
        self.comfort_anchor = float(a("comfort_anchor_c", 23.0))
        self.comfort_dp_rate = float(a("comfort_dp_rate_per_sleeper_c_per_h", 0.5))
        self.comfort_knee = float(a("comfort_dp_knee_c", 12.0))
        self.comfort_penalty = float(a("comfort_dp_penalty_per_c", 0.15))
        self.comfort_second_sleeper = float(a("comfort_second_sleeper_c", 0.5))
        # --- sleep-plan advisory (sensor.sleep_plan): cheapest path to comfortable sleep.
        # Read-only, command-free; published every tick. Windows (cost 0) vs AC (kWh * spot
        # price + noise penalty). Outdoor humidity + the seven window contacts feed it.
        self.outdoor_rh_entity = a("outdoor_rh_entity", "sensor.gw2000a_humidity")
        # NAME=ENTITY strings, NOT a yaml dict: a new dict-valued key trips AppDaemon 4.5's
        # config-scan deep_compare (KeyError -> full restart of every app); a list value is
        # safe and reloads only this app. Parsed back into a {name: entity} map here.
        raw_windows = a("window_contact_entities", [
            "bedroom=binary_sensor.bedroom_window_contact",
            "bathroom=binary_sensor.bathroom_window_contact",
            "dining 1=binary_sensor.dining_room_window_1_contact",
            "dining 2=binary_sensor.dining_room_window_2_contact",
            "dining 3=binary_sensor.dining_room_window_3_contact",
            "kitchen=binary_sensor.kitchen_window_contact",
            "living room=binary_sensor.living_room_window_contact",
            "kristines room=binary_sensor.kristines_room_window_contact",
        ])
        if isinstance(raw_windows, dict):
            self.window_contact_entities = dict(raw_windows)   # tolerate a dict (tests/back-compat)
        else:
            self.window_contact_entities = {}
            for item in raw_windows or []:
                name, _, ent = str(item).partition("=")
                if ent:
                    self.window_contact_entities[name.strip()] = ent.strip()
        # The sleep-plan's reality anchor is the BEDROOM ZONE only (its floor + mid wall + the
        # kitchen wall it conducts against), computed inline in _publish_sleep_plan. The A/C is
        # bedroom-only and the bedroom is its OWN thermal zone (user 2026-07-22): the far side
        # of the apartment (living/dining) can be a different temperature, so those rooms must
        # NOT ground the bedroom's projection.
        self.sleep_plan_entity = a("sleep_plan_entity", "sensor.sleep_plan")
        self.ac_noise_penalty_kr = float(a("ac_noise_penalty_kr", 0.5))
        # Wake-time display (Tonight card + push copy): the enabled alarm, else the
        # day-type fallback (07:00 workdays / 09:00 weekends -- user 2026-07-29). Display
        # only; actuation still plans against sleep_hours, never against the clock.
        self.alarm_time_entity = a("alarm_time_entity", "input_datetime.wakeup_bedroom")
        self.alarm_enabled_entity = a("alarm_enabled_entity", "input_boolean.wakeup_bedroom")
        self.fallback_workday = a("fallback_time_workday", "07:00:00")
        self.fallback_weekend = a("fallback_time_weekend", "09:00:00")
        # --- live session-cost metering + estimator calibration (validated 2026-07-21: the
        # sleep-plan's AC estimate was 4-8x too LOW against the Shelly meter -- it priced ONE
        # deficit-closing run at the single cheapest slot, ~1.5 kWh/~0.7 kr, while the plug
        # metered a real session's 3.5 kWh / ~4.2 kr spot. ac_energy_entity is the Shelly
        # cumulative kWh counter -- the ONLY reliable AC energy meter in this home;
        # _track_session_cost meters every real session against it every tick.
        # session_energy_factor/est_price_slots remain as the pre-metering theoretical-estimate
        # knobs (session_energy_factor is no longer read by plan_sleep -- see kwh_per_deg below
        # -- kept for any other caller; est_price_slots is now the pricing FLOOR/fallback when
        # the deficit isn't known yet, see _publish_sleep_plan).
        self.ac_energy_entity = a("ac_energy_entity", "sensor.ac_plug_energy")
        self.session_energy_factor = float(a("session_energy_factor", 2.5))
        self.est_price_slots = int(a("est_price_slots", 8))
        # Scaling cost model (2026-07-29 rebuild -- replaces the flat night_cost_ema EMA,
        # which 3 finalized 0.00 kWh sessions had poisoned down to 0.36 kr while 15 metered
        # days, 2026-07-10..29, actually averaged 4.50 kr). kwh_per_deg is kWh spent per
        # degree (C) of pre-cool deficit closed, learned from real sessions
        # (_finalize_session) and seeded from that same 15-day window: genuine cooling
        # nights ran 2.7-7.4 kWh for ~3-4C deficits (e.g. 2026-07-17: 6.27 kWh / 10.61 kr).
        # session_min_kwh excludes trivial/no-op sessions (armed+deployed but the compressor
        # never actually ran) from EVERY learned value -- they carry no signal about what
        # cooling costs, only noise.
        self.kwh_per_deg_default = float(a("kwh_per_deg_default", 1.6))
        self.session_min_kwh = float(a("session_min_kwh", 0.5))
        # --- fixed params (not user-facing) ---
        self.default_ceiling = float(a("default_night_ceiling", 23.0))
        self.min_temp = float(a("min_temp", 16.0))            # hardware floor; never drive below
        self.sleep_hours = float(a("sleep_hours", 8.0))
        # Seed cool rate; the REAL rate is learned per run into self._cool_cph (see
        # _track_cool_rate). 2026-07-29 measured 1.0C in 31 engaged min = ~1.9 C/h against
        # this 1.0 default, so every plan asked for ~2x the minutes it needed -- which
        # over-booked slots, dragged a marginal expensive hour into the run, and then
        # dropped it again mid-run when reality closed the deficit faster (the "start 11:00,
        # stop 11:31, wait for 12:00" the user asked about).
        self.floor_cool_cph = float(a("floor_cool_cph", 1.0))     # seed only; learned at runtime
        self.cool_cph_min = float(a("cool_cph_min", 0.3))         # sanity band for a learned rate
        self.cool_cph_max = float(a("cool_cph_max", 4.0))
        self.cool_rate_min_engaged = float(a("cool_rate_min_engaged_min", 20.0))
        # Only learn the rate while the floor still has real headroom above the learned
        # feasible limit. Near the limit the floor crawls (0.2-0.3C/h) because of the WALL,
        # not the machine -- 2026-07-29 those crawl segments dragged the learned rate
        # 1.68 -> 0.44 C/h overnight, so the next morning a 4.4C job priced as ~600 min
        # and the planner (correctly, given the false belief) ran from 08:38 through
        # morning prices instead of waiting for the cheap afternoon.
        self.rate_learn_min_headroom = float(a("rate_learn_min_headroom", 0.7))
        # Crawl-regime rate for PRICING the last stretch above the wall (measured 0.2-0.5
        # C/h on 2026-07-29; the knee is the same rate_learn_min_headroom boundary).
        self.crawl_rate_cph = float(a("crawl_rate_cph", 0.4))
        # Commitment hysteresis: while ALREADY cooling, keep going if the current slot costs
        # no more than the priciest slot we DID choose, plus this margin (kr/kWh). Rank slack
        # is useless here -- with a flat cheap evening the current slot ranks behind dozens of
        # equally-cheap ones -- but price proximity is exactly the question: don't abandon a
        # run to chase a saving of pennies (2026-07-29: 0.64 vs 0.50 kr/kWh = 0.02 kr for the
        # slot), while still stopping for a genuinely expensive stretch.
        self.commit_price_margin = float(a("commit_price_margin", 0.15))
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
        # get_forecasts flaps empty around HA restarts with no error (found 2026-07-30:
        # the model published no prediction on 7 of 11 mornings) -- reuse a stale forecast
        # for up to this many hours rather than fall back to the legacy kitchen proxy;
        # yesterday-evening physics beats that proxy for ~6h (see _get_forecast).
        self.wm_forecast_reuse_h = float(a("wm_forecast_reuse_h", 6.0))
        self.wm_b0 = float(a("wm_b0", 15.797))
        self.wm_b_solar = float(a("wm_b_solar", 0.0162))
        self.wm_b_vent = float(a("wm_b_vent", 0.198))
        self.wm_vent_knee = float(a("wm_vent_knee", 24.0))
        self.wm_b_prev = float(a("wm_b_prev", 0.287))
        self.wm_safety_margin = float(a("wm_safety_margin", 0.0))
        self.wm_nowcast_relief = float(a("wm_nowcast_relief", 1.5))
        # Sleep-plan GROUNDING (advisory only -- never touches actuation; see
        # _publish_sleep_plan / cm.grounded_equilibrium). On a cool/cooling night the sealed
        # room drifts toward the cool night apartment, not the weather model's DAYTIME peak,
        # so the plan caps the equilibrium at apartment_now + wm_reality_margin. A genuinely
        # warm night (night_outdoor >= comfort_limit - wm_warm_night_margin) keeps the raw
        # weather value so pre-cool-ahead-of-a-hot-night is preserved.
        self.wm_reality_margin = float(a("wm_reality_margin", 1.0))
        self.wm_warm_night_margin = float(a("wm_warm_night_margin", 1.0))
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
        # Evening rescue advisory (disarmed): if the night is genuinely at risk but still
        # rescuable before bed, send ONE notification -- never a climate command -- so the
        # user can arm + redeploy the unit. Gated to the evening window, a real unmet
        # pre-cool deficit that still fits before the cutoff hour, and (optionally) the user
        # being home. One per calendar day; the daily reset re-arms it (see
        # _maybe_evening_rescue / _rescue_notified_date).
        self.rescue_enabled = bool(a("rescue_enabled", True))
        self.rescue_from_hour = int(a("rescue_from_hour", 16))
        self.rescue_to_hour = int(a("rescue_to_hour", 23))
        self.rescue_deficit_min = float(a("rescue_deficit_min", 0.5))
        self.rescue_home_entity = a("rescue_home_entity", "person.mikkel")
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
        # Learn the achieved floor minimum from EVERY night with at least this much
        # engaged time (see _finalize_night); shorter runs bottom out on the clock,
        # not on capacity, and would teach a falsely shallow floor.
        self.feasible_learn_min_engaged = float(a("feasible_learn_min_engaged", 120))
        # How far BELOW the learned feasible floor a night may probe. Widened 0.3 -> 1.0
        # (user 2026-07-29, "probe deeper... but balance it"): measured over 11 nights, 1C
        # deeper at seal buys 0.73C cooler sleeping-zone air at wake, so the old 0.3 was
        # leaving real morning comfort unclaimed. Balanced by construction -- _reach_target
        # takes max(physics target, feasible - probe), so a probe can only ever chase depth
        # the night ACTUALLY needs, never depth for its own sake; a mild night whose target
        # is 21.5 still stops at 21.5. Saturation (priority 1) pulls back the nights the
        # room genuinely will not go there, and re-teaches the learner.
        self.feasible_probe_c = float(a("feasible_probe_c", 1.0))
        self._sat_min: Optional[float] = None      # session floor minimum while pursuing
        self._sat_noprog_min = 0.0                 # engaged minutes without a new minimum
        self._saturated = False
        self._night_floor_min: Optional[float] = None   # tonight's deepest floor while engaged
        self._night_engaged_min = 0.0                   # tonight's engaged minutes
        self._learned_tonight = False                   # saturation already taught tonight
        self._last_want = False                    # was the previous eval trying to cool?
        self._last_eval_at: Optional[datetime] = None
        # Learned cooling rate (C/h) + its accumulator. The floor sensor only reports every
        # ~30 min, so a per-tick delta is mostly zeros punctuated by a big step: accumulate
        # engaged minutes against a reference reading and learn when the reading actually
        # moves. Persisted like rise_frac / feasible_floor.
        self._cool_cph: Optional[float] = None
        self._cool_cph_samples = 0
        self._rate_ref_floor: Optional[float] = None
        self._rate_engaged_min = 0.0
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
        # Latest sleep-plan verdict (stashed by _publish_sleep_plan each tick); the evening
        # rescue DELIVERS this instead of computing its own projection -- one brain.
        self._last_plan = None
        self._last_wake_floor = None  # last coast-learn's end floor -- the morning receipt
        # One evening-rescue advisory per calendar day (YYYY-MM-DD, or None). The rollover
        # is implicit: a new day's date != the stored one, so the next qualifying evening
        # re-arms without an explicit reset. Persisted so a reload/HA restart mid-evening
        # doesn't re-notify.
        self._rescue_notified_date: Optional[str] = None
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
        # Fail handling (found 2026-07-30: weather.get_forecasts intermittently returns an
        # empty payload with NO error, blind on 7 of 11 mornings -- see _get_forecast).
        # _fc_fail_at backs off repeat service calls for 120s after ANY failure (fetch
        # exception or empty parse); _fc_warned_at rate-limits the empty-payload WARNING to
        # once per 30 min so a ticking eval loop can't spam the log; _fc_served_age_min is
        # the whole-minute age of whatever _get_forecast last actually served (0-ish when
        # fresh, up to wm_forecast_reuse_h*60 when stale, None when nothing was served) --
        # published on the status entity for visibility (see _attrs).
        self._fc_fail_at: Optional[datetime] = None
        self._fc_warned_at: Optional[datetime] = None
        self._fc_served_age_min: Optional[int] = None
        # Live session-cost metering (see the ac_energy_entity/session_min_kwh/
        # kwh_per_deg_default config block above): _session_kwh0/_session_last_counter track
        # the Shelly counter baseline while a session is open (both None = no active
        # session); _session_kwh/_session_cost accumulate the running totals.
        # _last_session_kwh/_last_session_cost/_night_cost_ema/_night_cost_samples are the
        # finalized, live/display readout (night_cost_ema no longer drives plan_sleep's
        # estimate -- see kwh_per_deg below). Persisted so a reload mid-session (or a
        # learned value) survives an HA/AppDaemon restart.
        self._session_kwh0: Optional[float] = None
        self._session_last_counter: Optional[float] = None
        self._session_kwh: float = 0.0
        self._session_cost: float = 0.0
        self._last_session_kwh: Optional[float] = None
        self._last_session_cost: Optional[float] = None
        self._night_cost_ema: Optional[float] = None
        self._night_cost_samples: int = 0
        # Pre-cool deficit (C) captured the moment a session opens (see _track_session_cost),
        # from the sleep plan's own last-published verdict -- the anchor kwh_per_deg is
        # learned against. self._kwh_per_deg starts at the seeded default and is overwritten
        # by the learned EMA once a qualifying session has been metered (see
        # _finalize_session); _kwh_per_deg_samples counts those qualifying sessions.
        self._session_deficit0: Optional[float] = None
        self._session_started_at: Optional[str] = None
        self._kwh_per_deg: float = self.kwh_per_deg_default
        self._kwh_per_deg_samples: int = 0
        # Day-history for the dashboard bar (2026-07-30): which stretches the AC actually
        # spent ENGAGED today (cooling or stall-burping), so the Tonight card can draw what
        # actually happened, not just the current status. Tracked from a status TRANSITION
        # at the one publish site (_publish), compared against the previously PUBLISHED
        # status (_last_pub_status) rather than the internal want/decision -- see
        # _track_cool_intervals. Persisted (see _load_state/_save_state) so a reload mid-run
        # doesn't lose the open interval's start.
        self._cool_intervals: list = []
        self._last_pub_status: Optional[str] = None
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

    def _cheapest_to(self, pm, now, deadline, fallback):
        """Cheapest 15-min slot price between now and deadline (advisory pricing for the
        sleep plan). Falls back to `fallback` (price_now) when no slot horizon remains."""
        total = int((deadline - now).total_seconds() // 900)
        if total <= 0:
            return fallback
        prices = [self._price_for(pm, now + timedelta(minutes=15 * k), fallback)
                  for k in range(total)]
        return min(prices) if prices else fallback

    def _blended_cheap(self, pm, now, deadline, fallback, k):
        """Mean of the k cheapest 15-min slot prices between now and deadline (advisory
        pricing for the sleep plan). A real session doesn't run in a single cheapest slot --
        it re-cools across the night (re-warm top-ups, stall-burps, the post-midnight cheap-
        power chase) -- so blending the k cheapest slots (est_price_slots) is a closer stand-
        in for what a real session actually pays than the single cheapest one (_cheapest_to),
        which under-priced the 2026-07-21 session by ~2.5x on price alone. Falls back to
        `fallback` (price_now) when no slot horizon remains. Modeled on _cheapest_to."""
        total = int((deadline - now).total_seconds() // 900)
        if total <= 0:
            return fallback
        prices = [self._price_for(pm, now + timedelta(minutes=15 * m), fallback)
                  for m in range(total)]
        if not prices:
            return fallback
        chosen = sorted(prices)[:max(1, min(k, len(prices)))]
        return sum(chosen) / len(chosen)

    # ---------- learned warm-up indicator ----------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
            self._rise_frac = float(d.get("rise_frac", self._rise_frac))
            lwf = d.get("last_wake_floor")
            self._last_wake_floor = float(lwf) if lwf is not None else None
            nfm = d.get("night_floor_min")
            self._night_floor_min = float(nfm) if nfm is not None else None
            self._night_engaged_min = float(d.get("night_engaged_min", 0.0))
            self._learned_tonight = bool(d.get("learned_tonight", False))
            cc = d.get("cool_cph")
            self._cool_cph = float(cc) if cc is not None else None
            self._cool_cph_samples = int(d.get("cool_cph_samples", 0))
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
            self._rescue_notified_date = d.get("rescue_notified_date")
            sk0 = d.get("session_kwh0")
            self._session_kwh0 = float(sk0) if sk0 is not None else None
            slc = d.get("session_last_counter")
            self._session_last_counter = float(slc) if slc is not None else None
            ssa = d.get("session_started_at")
            self._session_started_at = str(ssa) if ssa is not None else None
            self._session_kwh = float(d.get("session_kwh", 0.0))
            self._session_cost = float(d.get("session_cost", 0.0))
            lsk = d.get("last_session_kwh")
            self._last_session_kwh = float(lsk) if lsk is not None else None
            lsc = d.get("last_session_cost")
            self._last_session_cost = float(lsc) if lsc is not None else None
            nce = d.get("night_cost_ema")
            self._night_cost_ema = float(nce) if nce is not None else None
            self._night_cost_samples = int(d.get("night_cost_samples", 0))
            kpd = d.get("kwh_per_deg")
            self._kwh_per_deg = float(kpd) if kpd is not None else self._kwh_per_deg
            self._kwh_per_deg_samples = int(d.get("kwh_per_deg_samples", self._kwh_per_deg_samples))
            raw_intervals = d.get("cool_intervals", self._cool_intervals)
            if isinstance(raw_intervals, list):
                self._cool_intervals = [v for v in
                                        (self._valid_cool_interval(x) for x in raw_intervals)
                                        if v is not None]
            else:
                self._cool_intervals = []
        except Exception:
            pass

    @staticmethod
    def _valid_cool_interval(item):
        """One cool_intervals_today entry, defensively validated on load: a 2-item
        list/tuple whose start parses as ISO and whose end is None or itself
        ISO-parseable. Returns the normalized [start, end] list, or None to drop a
        malformed entry rather than let one bad line crash the whole state load."""
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        start, end = item
        if not isinstance(start, str):
            return None
        try:
            datetime.fromisoformat(start)
        except (TypeError, ValueError):
            return None
        if end is not None:
            if not isinstance(end, str):
                return None
            try:
                datetime.fromisoformat(end)
            except (TypeError, ValueError):
                return None
        return [start, end]

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"rise_frac": self._rise_frac, "rise_samples": self._rise_samples,
                           "last_wake_floor": self._last_wake_floor,
                           "night_floor_min": self._night_floor_min,
                           "night_engaged_min": round(self._night_engaged_min, 1),
                           "learned_tonight": self._learned_tonight,
                           "cool_cph": self._cool_cph,
                           "cool_cph_samples": self._cool_cph_samples,
                           "lightout": self._lightout,
                           "feasible_floor": self._feasible_floor,
                           "feasible_samples": self._feasible_samples,
                           "prev_kitchen_max": self._prev_kitchen_max,
                           "kitchen_max_today": self._kitchen_max_today,
                           "kitchen_max_date": self._kitchen_max_date,
                           "rescue_notified_date": self._rescue_notified_date,
                           "session_kwh0": self._session_kwh0,
                           "session_last_counter": self._session_last_counter,
                           "session_started_at": self._session_started_at,
                           "session_kwh": self._session_kwh,
                           "session_cost": self._session_cost,
                           "last_session_kwh": self._last_session_kwh,
                           "last_session_cost": self._last_session_cost,
                           "night_cost_ema": self._night_cost_ema,
                           "night_cost_samples": self._night_cost_samples,
                           "kwh_per_deg": self._kwh_per_deg,
                           "kwh_per_deg_samples": self._kwh_per_deg_samples,
                           "cool_intervals": self._cool_intervals}, f)
        except Exception as e:
            self.log(f"state save failed ({e}) -- continuing in-memory", level="WARNING")

    def _equilibrium(self, kitchen, mid, floor):
        """Where the sealed sleeping zone drifts overnight. Driven by the neighbour wall (kitchen)
        and the room's own warm baseline + the sleeper. Conservative: take the warmest sensible
        reading (errs deep -> safe). Thin wrapper over cm.legacy_equilibrium (byte-identical:
        empty_fallback defaults to 24.5) so every call site stays untouched."""
        return cm.legacy_equilibrium(kitchen, mid, floor, self.person_offset)

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
        yields nothing and there's no stale forecast left to reuse (caller degrades to
        legacy / full clear-sky). Never raises, never stalls the eval loop (bounded
        wait_for).

        weather.get_forecasts intermittently returns an empty payload with NO error at all
        (found 2026-07-30: the weather model published no prediction on 7 of 11 mornings) --
        an empty parse is now treated exactly like a fetch exception: both mark
        _fc_fail_at and fall through to STALE reuse (the last good forecast, while under
        wm_forecast_reuse_h old) instead of the caller's hard None degradation --
        yesterday-evening physics beats the legacy kitchen proxy for a few hours. A short
        fail-backoff (120s) then keeps one broken tick from firing repeated service calls:
        4+ methods (_solar_mean_today, _outdoor_max_today, _night_outdoor_min,
        _weather_equilibrium) call this every eval tick."""
        if (self._fc_cache is not None and self._fc_cache_at is not None
                and (now - self._fc_cache_at).total_seconds() < 1800):
            self._fc_served_age_min = int((now - self._fc_cache_at).total_seconds() // 60)
            return self._fc_cache
        if self._fc_fail_at is not None and (now - self._fc_fail_at).total_seconds() < 120:
            return self._serve_stale_forecast(now, warn=False)
        try:
            resp = await asyncio.wait_for(
                self.call_service("weather/get_forecasts",
                                  entity_id=self.weather_forecast_entity,
                                  type="hourly", return_response=True),
                timeout=12)
        except Exception as e:
            self.log(f"weather-model forecast fetch failed ({e})", level="WARNING")
            self._fc_fail_at = now
            return self._serve_stale_forecast(now, warn=False)
        node = cm.parse_forecast_envelope(resp, self.weather_forecast_entity)
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
            self._fc_fail_at = now
            return self._serve_stale_forecast(now, warn=True)
        self._fc_cache, self._fc_cache_at = out, now
        self._fc_fail_at = None
        self._fc_warned_at = None
        self._fc_served_age_min = 0
        return out

    def _serve_stale_forecast(self, now, warn):
        """Common tail for _get_forecast's failure paths (fetch exception, empty parse, or
        a fail-backoff skip): reuse the last good forecast while it's under
        wm_forecast_reuse_h old, else degrade to None -- the legacy (pre-2026-07-30)
        behaviour. Always sets _fc_served_age_min (see its init comment for the
        0-ish/up-to-360/None ranges). `warn` is only set by the empty-parse path -- a fetch
        exception already logs its own message, and a backoff skip deliberately stays
        silent, since it didn't even attempt a fetch -- and is itself rate-limited to one
        WARNING per 30 min (_fc_warned_at) so a ticking eval loop can't spam the log the
        way energidataservice's "empty dataset!" once did."""
        age_min = ((now - self._fc_cache_at).total_seconds() / 60.0
                   if self._fc_cache_at is not None else None)
        stale_ok = age_min is not None and age_min <= self.wm_forecast_reuse_h * 60.0
        if warn and (self._fc_warned_at is None
                     or (now - self._fc_warned_at).total_seconds() >= 1800):
            self._fc_warned_at = now
            if stale_ok:
                self.log(f"weather-model forecast returned an empty payload "
                         f"({self.weather_forecast_entity}); reusing forecast from "
                         f"{age_min:.0f} min ago", level="WARNING")
            else:
                self.log(f"weather-model forecast returned an empty payload "
                         f"({self.weather_forecast_entity}); no cached forecast to reuse",
                         level="WARNING")
        if stale_ok:
            self._fc_served_age_min = int(age_min)
            return self._fc_cache
        self._fc_served_age_min = None
        return None

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

    async def _night_outdoor_min(self, now):
        """Tonight's minimum outdoor temperature (advisory grounding input for the sleep
        plan -- see cm.grounded_equilibrium): the coldest forecast temp from now through the
        next 07:00. Falls back to the current outdoor reading when the forecast is missing or
        has no rows in the overnight window -- current is usually >= the overnight min, so
        the fallback errs toward keeping the weather value (warm/safe). None only if neither
        source yields anything."""
        cur = await self._num(self.outdoor_sensor, None)
        fc = await self._get_forecast(now)
        if not fc:
            return cur
        morning = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now.hour >= 7:
            morning += timedelta(days=1)
        temps = [row["temp"] for row in fc if now <= row["dt"] <= morning]
        if not temps:
            return cur
        night_min = min(temps)
        return night_min if cur is None else min(night_min, cur)

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
            e_apartment = cm.model_d_apartment(
                solar_mean, outdoor_max, self._prev_kitchen_max,
                cm.ModelDCoeffs(self.wm_b0, self.wm_b_solar, self.wm_b_vent,
                                self.wm_vent_knee, self.wm_b_prev))
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
        F0 = (cap - E*r)/(1 - r). Clamp to [min_temp, ceiling]. Thin wrapper over
        cm.calc_floor_target (byte-identical) so the two call sites stay untouched."""
        return cm.calc_floor_target(E, ceiling, self._rise_frac, self.zone_offset, self.min_temp)

    def _schedule(self, now, deadline, minutes_needed, price_at, already_cooling=False):
        """Reserve the cheapest `minutes_needed` of 15-min slots between now and deadline (midnight -
        see _next_midnight). Cool NOW if the current slot is one of them, or if there isn't time left
        to wait. Returns (cool_now, next_start, run_min, est_cost, windows) -- windows being the
        chosen set merged into [[start, end], ...] wall-clock stretches for the dashboard bar.

        COMMITMENT (user 2026-07-29, "why start at 11:00 for 30 min then wait for 12:00?"): the
        plan is re-solved from scratch every tick, so as cooling closes the deficit the
        requirement shrinks and the slot we are CURRENTLY RUNNING IN can drop out of the
        cheapest set -- stopping mid-run to wait for a slot barely cheaper. While already
        cooling we therefore keep going when the current slot costs no more than the priciest
        chosen slot + commit_price_margin. `chosen` (and so next_start/run_min/est) still
        reflects the strict cheapest-N plan; only the keep-going decision gets the slack."""
        total = int((deadline - now).total_seconds() // 900)
        if total <= 0 or minutes_needed <= 0:
            return False, None, 0, 0.0, []
        need = min(total, int((minutes_needed + 14.999) // 15))
        slots = [now + timedelta(minutes=15 * k) for k in range(total)]
        order = sorted(range(total), key=lambda k: price_at(slots[k]))
        chosen = sorted(order[:need])
        cool_now = (0 in chosen) or (need >= total)
        if not cool_now and already_cooling and chosen:
            worst_chosen = max(price_at(slots[k]) for k in chosen)
            cool_now = price_at(slots[0]) <= worst_chosen + self.commit_price_margin
        next_start = slots[chosen[0]] if chosen else None
        est = sum(self.cool_kw * 0.25 * price_at(slots[k]) for k in chosen)
        # The chosen set as merged wall-clock windows, so the dashboard can draw the actual
        # run / hold-for-price / resume plan instead of projecting one contiguous block from
        # now (user 2026-07-30: the bar showed cooling straight through the 19-21 price peak
        # this very set was skipping).
        windows = []
        for k in chosen:
            start, end = slots[k], slots[k] + timedelta(minutes=15)
            if windows and windows[-1][1] == start:
                windows[-1][1] = end
            else:
                windows.append([start, end])
        return cool_now, next_start, need * 15, round(est, 2), windows

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
        # The morning receipt: what the floor actually woke at (coast-window end ~= wake).
        self._last_wake_floor = round(floor, 1)
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

    # ---------- learned cooling rate (how FAST the floor actually drops) ----------
    def _cool_rate(self):
        """The rate to plan with: the learned C/h once we have a sample, else the seed."""
        return self._cool_cph if self._cool_cph else self.floor_cool_cph

    def _cooling_minutes(self, floor, target):
        """Two-regime engaged-minutes estimate (see cm.cooling_minutes): fast rate down to
        the knee (learned wall + headroom), crawl rate below it. The wall only counts once
        it is trusted (feasible_min_samples), mirroring _reach_target/_plan_floor_limit."""
        wall = (self._feasible_floor
                if self._feasible_floor is not None
                and self._feasible_samples >= self.feasible_min_samples else None)
        return cm.cooling_minutes(floor, target, self._cool_rate(), wall,
                                  self.rate_learn_min_headroom, self.crawl_rate_cph)

    def _track_cool_rate(self, floor, engaged_min, cooling):
        """Learn the real floor cool rate (C/h) from engaged cooling time.

        The floor sensor only reports every ~30 min, so per-tick deltas are mostly zero with
        an occasional big step. Accumulate engaged minutes against a reference reading and
        learn only when the reading actually moves, requiring cool_rate_min_engaged minutes
        so a coarse step isn't divided by a tiny window. Any non-cooling tick resets the
        accumulator (coast time mixed into the window would understate the rate), as does the
        floor warming above the reference.

        Why it matters (2026-07-29): a 1.0 C/h seed against a real ~1.9 C/h made every plan
        ask for double the minutes, which over-booked price slots -- see the floor_cool_cph
        init comment. EMA alpha 0.4, clamped to [cool_cph_min, cool_cph_max]."""
        if floor is None or not cooling:
            self._rate_ref_floor = floor if cooling else None
            self._rate_engaged_min = 0.0
            return
        if self._rate_ref_floor is None:
            self._rate_ref_floor = floor
            self._rate_engaged_min = 0.0
            return
        # Near the feasible limit the drop measures the wall, not the machine: skip the
        # sample entirely (and re-anchor) so crawl segments can't teach a false slow rate.
        if (self._feasible_floor is not None
                and floor - self._feasible_floor < self.rate_learn_min_headroom):
            self._rate_ref_floor = floor
            self._rate_engaged_min = 0.0
            return
        self._rate_engaged_min += max(0.0, engaged_min)
        drop = self._rate_ref_floor - floor
        if drop <= 0.05:
            if drop < -self.sat_reset_rise:      # warmed well past the reference -> restart
                self._rate_ref_floor = floor
                self._rate_engaged_min = 0.0
            return
        if self._rate_engaged_min < self.cool_rate_min_engaged:
            return                                # too short a window to divide by
        window = self._rate_engaged_min
        obs = max(self.cool_cph_min, min(self.cool_cph_max, drop / (window / 60.0)))
        self._cool_cph = obs if self._cool_cph is None else round(
            0.6 * self._cool_cph + 0.4 * obs, 3)
        self._cool_cph_samples += 1
        self._rate_ref_floor = floor
        self._rate_engaged_min = 0.0
        self._save_state()
        self.log(f"Learned cool rate: {obs:.2f} C/h observed ({drop:.1f}C in "
                 f"{window:.0f} engaged min); rate now {self._cool_cph:.2f} C/h "
                 f"(n={self._cool_cph_samples})", level="INFO")

    def _plan_floor_limit(self):
        """The deepest floor the ADVISORY plan should assume (and price). Mirrors
        _reach_target's historical feasible-floor cap so sensor.sleep_plan sizes the same
        job the controller will actually run; without it the plan priced a run to min_temp
        that the floor has demonstrably never reached."""
        if (self._feasible_floor is not None
                and self._feasible_samples >= self.feasible_min_samples):
            return max(self.min_temp, self._feasible_floor - self.feasible_probe_c)
        return self.min_temp

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
          3. The historical feasible-floor cap, minus feasible_probe_c -- elsewhere
             (daytime/evening), avoid paying peak prices chasing depth history says won't
             hold, while still probing a little past it because a deeper seal measurably
             improves the WAKE temperature (see feasible_probe_c). max(target, ...) keeps
             this honest: the probe can only chase depth the night actually needs."""
        if saturated and self._sat_min is not None:
            return max(target, self._sat_min)
        if now.hour < 6:
            return self.min_temp
        if self._feasible_floor is not None and self._feasible_samples >= self.feasible_min_samples:
            return max(target, self._feasible_floor - self.feasible_probe_c)
        return target

    def _learn_feasible(self, floor_min, why="saturated"):
        """EMA the observed can't-go-lower floor across nights, so future plans stop
        promising (and pricing) depth the unit can't deliver.

        The 1/min(6, n+1) weight averages the first six observations and then settles into a
        stable 1/6 EMA -- a CENTRAL tendency across the 19.2-21.2C spread of achieved minima,
        which keeps the planner from sizing every night against a lucky-night depth it usually
        can't reach (over-asking inflates minutes_needed and drags expensive slots into the
        plan -- the failure feasible_floor exists to prevent).

        NOTE (2026-07-29): a deeper seal is NOT comfort-neutral -- measured across 11
        bed-occupancy nights, 1C deeper at seal buys 0.73C cooler sleeping-zone air at wake
        (0.48C on the floor, R2 0.63). So the central tendency trades real morning comfort for
        scheduling safety; whether to bias this toward the achievable depth instead is the
        user's call, not an assumption to bake in."""
        n = self._feasible_samples
        w = 1.0 / min(6, n + 1)
        self._feasible_floor = round(
            floor_min if self._feasible_floor is None
            else (1 - w) * self._feasible_floor + w * floor_min, 2)
        self._feasible_samples = n + 1
        self._learned_tonight = True
        self._save_state()
        self.log(f"Feasible floor tonight: {floor_min:.1f}C ({why}); learned limit now "
                 f"{self._feasible_floor:.1f}C (n={self._feasible_samples})", level="INFO")

    def _finalize_night(self):
        """Learn from EVERY cooling night's achieved floor minimum, not only from the rare
        saturation event.

        Why (2026-07-29): _track_progress only calls _learn_feasible after sat_engaged_min
        (90) engaged minutes with no new minimum, which had fired exactly ONCE in three weeks
        -- so the planner was sizing and pricing every night off a single stale sample (20.5)
        while the history held 12 perfectly good nights (mean 20.27). Closing out a night with
        real engaged time is itself evidence of how deep this unit gets.

        Skipped when the night barely ran (feasible_learn_min_engaged) -- a short run's minimum
        is bounded by the clock, not by capacity, and would teach a falsely shallow floor -- and
        when saturation already learned tonight, so one night never counts twice."""
        floor_min, engaged = self._night_floor_min, self._night_engaged_min
        self._night_floor_min = None
        self._night_engaged_min = 0.0
        learned = self._learned_tonight
        self._learned_tonight = False
        if learned or floor_min is None or engaged < self.feasible_learn_min_engaged:
            self._save_state()
            return
        self._learn_feasible(floor_min, why=f"{engaged:.0f} engaged min tonight")
        self._learned_tonight = False        # the night is over either way

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

    def _track_session_cost(self, deployed, price_now, counter):
        """Live session-cost metering from the Shelly plug's cumulative kWh counter -- the
        ONLY reliable AC energy meter in this home (see the ac_energy_entity config comment
        / 2026-07-21 calibration). Runs every tick regardless of arm/deploy state (the unit
        can flap), so a session's energy is never truncated by a brief disconnect. SYNC and
        pure-ish (only touches _session_*/_save_state) -- unit-testable without a running
        AppDaemon.

        counter is the current cumulative kWh reading (None -- sensor unavailable -- is a
        no-op). A session starts the moment the AC is deployed with none already open, then
        accumulates kWh/kr from the counter's delta each tick until _finalize_session() closes
        it out. A negative delta (counter reset, e.g. a Shelly reboot) re-baselines instead of
        subtracting energy that was never actually used.

        Session start also captures _session_deficit0 -- the pre-cool deficit (C) the sleep
        plan last published (self._last_plan, stashed by _publish_sleep_plan each tick) --
        the anchor kwh_per_deg learning divides the metered kWh by (see _finalize_session).
        Reading it here rather than threading a fresh deficit argument through this call site
        is simpler and just as robust: the plan publishes every tick, so this is always the
        freshest deficit known at the moment cooling actually starts. None if no plan has
        published yet (e.g. right after a cold start)."""
        if counter is None:
            return
        if deployed and self._session_kwh0 is None:
            self._session_kwh0 = counter
            self._session_last_counter = counter
            self._session_kwh = 0.0
            self._session_cost = 0.0
            self._session_deficit0 = (self._last_plan or {}).get("deficit")
            self._session_started_at = datetime.now().astimezone().isoformat()
            self._save_state()
            self.log(f"AC session metering started at {counter:.3f} kWh", level="INFO")
            return
        if self._session_kwh0 is None:
            return
        delta = counter - self._session_last_counter
        if delta < 0:            # counter reset (e.g. Shelly reboot) -- re-baseline, don't go negative
            delta = 0.0
        self._session_kwh += delta
        self._session_cost += delta * (price_now if price_now is not None else 1.7)
        self._session_last_counter = counter
        if delta > 0:
            self._save_state()

    def _finalize_session(self):
        """Close out the metered session -- the AC-removed press, or the disarmed+undeployed
        fallback (user unplugged without pressing it first). Freezes the session totals as
        last night's numbers (_last_session_kwh/_last_session_cost -- still published, still
        the correct live/most-recent-night readout) and clears the session baseline so the
        next deploy starts fresh. A no-op if no session is open (including a second call in
        the same tick).

        Trivial sessions (armed+deployed but the compressor barely/never actually ran --
        session_min_kwh, default 0.5 kWh) are excluded from EVERY learned value: 3 finalized
        0.00 kWh sessions once dragged night_cost_ema down to 0.36 kr against a real ~4.50 kr
        metered average (2026-07-10..29) -- a trivial session carries no signal about what
        cooling actually costs, only noise. _last_session_kwh/_last_session_cost are still
        set and the baseline still clears, but neither learned value updates and neither
        sample count increments.

        On a qualifying session, night_cost_ema still EMAs (kept for the live/display
        readout -- see the module docstring) but plan_sleep no longer uses it to drive the
        estimate; kwh_per_deg (kWh spent per degree of pre-cool deficit closed) is what does
        now, EMA'd (alpha 0.4) from session_kwh / deficit0 -- the deficit captured at session
        START (see _track_session_cost/_session_deficit0). Skipped (not learned) if that
        deficit is missing or under 0.5C: too shallow to attribute the session's energy to
        reliably."""
        if self._session_kwh0 is None:
            return
        self._last_session_kwh = round(self._session_kwh, 3)
        self._last_session_cost = round(self._session_cost, 2)
        session_kwh = self._session_kwh
        deficit0 = self._session_deficit0
        self._session_kwh0 = None
        self._session_last_counter = None
        self._session_deficit0 = None
        self._session_started_at = None
        if session_kwh < self.session_min_kwh:
            self._save_state()
            self.log(f"Session skipped ({session_kwh:.3f} kWh < {self.session_min_kwh:.1f} "
                     f"kWh min -- trivial/no-op night) -- not learning from it", level="INFO")
            return
        ema = self._night_cost_ema
        self._night_cost_ema = (self._last_session_cost if ema is None
                                else round(0.6 * ema + 0.4 * self._last_session_cost, 2))
        self._night_cost_samples += 1
        if deficit0 is not None and deficit0 >= 0.5:
            obs = session_kwh / deficit0
            self._kwh_per_deg = round(
                obs if self._kwh_per_deg_samples == 0
                else 0.6 * self._kwh_per_deg + 0.4 * obs, 3)
            self._kwh_per_deg_samples += 1
        self._save_state()
        self.log(f"Session energy: {self._last_session_kwh:.2f} kWh, "
                 f"{self._last_session_cost:.2f} kr (night-cost EMA {self._night_cost_ema:.2f} kr, "
                 f"n={self._night_cost_samples}; kwh/deg {self._kwh_per_deg:.3f}, "
                 f"n={self._kwh_per_deg_samples})", level="INFO")

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

    async def _effective_ceiling(self, now):
        """Tonight's ceiling: the night-ceiling knob, optionally LOWERED for a humid/two-
        sleeper night -- bounded to at most comfort_max_reduction below the knob, never below
        min_temp, never raised above the knob. Returns (ceiling, ceiling_base). Shared by the
        armed decision path and the disarmed evening-rescue check so the two can't diverge.

        Computed LOCALLY via the shared climate_model comfort fns on the SAME entities/params
        bedroom_comfort uses (see the comfort_* config mirrored from bedroom_comfort.yaml) --
        this breaks the old ceiling_effective<->rise_frac cycle: smart_cooling no longer READS
        sensor.bedroom_comfort. Numerically identical to the value bedroom_comfort publishes
        (same fn, same inputs), so the driven target stays byte-identical; the only difference
        is freshness (live temp/RH here vs the comfort sensor's up-to-5-min-stale copy)."""
        ceiling_base = await self._num(self.night_ceiling_entity, self.default_ceiling)
        try:
            t_in = await self._num(self.comfort_temp_entity, None)
            rh_in = await self._num(self.comfort_rh_entity, None)
            homes = 0
            for p in self.comfort_persons:   # explicit loop: await can't live in a genexp
                if (await self._state(p)) == "home":
                    homes += 1
            sleepers = max(1, homes)
            dp = cm.dew_point_c(t_in, rh_in)
            dp_m = cm.project_morning_dp(dp, sleepers, cm.hours_until_morning(now),
                                         self.comfort_dp_rate)
            ce, _ = cm.effective_ceiling(self.comfort_anchor, dp_m, sleepers,
                                         self.comfort_knee, self.comfort_penalty,
                                         self.comfort_second_sleeper, self.comfort_max_reduction)
            ceiling = min(ceiling_base,
                          max(ce, ceiling_base - self.comfort_max_reduction, self.min_temp))
        except (TypeError, ValueError):
            ceiling = ceiling_base
        return ceiling, ceiling_base

    async def _maybe_evening_rescue(self, now, floor):
        """Disarmed evening advisory -- NEVER a climate command, and NEVER its own brain:
        it DELIVERS the sleep plan's verdict in the evening window, nothing more. Fires
        only when the CURRENT plan (same tick -- _publish_sleep_plan stashes _last_plan
        just before this runs) says "ac": a window genuinely can't fix tonight. Until
        2026-07-23 the rescue recomputed its own projection from the un-grounded kitchen
        proxy and pushed "deploy the AC" at a 21.6C bedroom while the plan said windows --
        two brains contradicting through different channels; now there is one.

        Remaining gates: enabled, evening window, a real pre-cool deficit (against the
        plan's own grounded equilibrium + limit) that still fits before the cutoff, user
        HOME, once per day. A push while away is pure stress -- nothing can be done from
        there (user 2026-07-23, firm) -- so a live not_home suppresses WITHOUT consuming
        the day: come home while the night is still saveable and the next tick delivers
        it. A dead/unknown presence sensor never counts as away. Silent no-op on error."""
        try:
            if not self.rescue_enabled:
                return
            if not (self.rescue_from_hour <= now.hour < self.rescue_to_hour):
                return
            today = now.strftime("%Y-%m-%d")
            if self._rescue_notified_date == today:
                return
            if floor is None:
                return
            plan = self._last_plan
            if not plan or plan.get("rec") != "ac":
                return   # the plan's remedy is windows/nothing -> no compressor push
            E = plan.get("equilibrium")
            ceiling = plan.get("limit")
            if E is None or ceiling is None:
                return
            target = self._calc_target(E, ceiling)
            deficit = floor - target
            if deficit < self.rescue_deficit_min:
                return
            mins = self._cooling_minutes(floor, target)
            # Still time to pre-cool the deficit away before the cutoff hour?
            if (self.rescue_to_hour - now.hour) * 60 < mins:
                return
            if self.rescue_home_entity:
                home = await self._state(self.rescue_home_entity)
                # Away = a stress push about something unactionable (user 2026-07-23) ->
                # suppress, but WITHOUT marking the day sent: the first tick after coming
                # home (still inside the window, deficit still feasible) delivers it.
                # A dead/unknown sensor is never treated as evidence of being away.
                if home not in (None, "unknown", "unavailable", "home"):
                    return
            peak = plan.get("peak")
            heading = (f"{peak:.1f}° heading vs the {ceiling:.1f}° limit" if peak is not None
                       else f"vs the {ceiling:.1f}° limit")
            await self._notify(
                f"Tonight needs the AC -- plug it in and arm Cool night. "
                f"About {mins:.0f} min of pre-cool ({heading}).")
            self._rescue_notified_date = today
            self._save_state()
        except Exception as e:
            self.log(f"evening rescue check failed ({e}) -- skipping", level="WARNING")

    async def _publish_sleep_plan(self, now, floor, e_active):
        """Publish sensor.sleep_plan -- the cheapest-path advisory (windows vs AC) for a
        comfortable night. ADVISORY ONLY: this issues ZERO climate/* commands. Runs every
        tick on every branch (disarmed, not-deployed, armed), placed BEFORE the arm gate so
        it cannot feed back into actuation. Wrapped so an advisory failure never breaks the
        tick or delays the actuation below it.

        Projects from e_active (weather Model D when live, else e_legacy) instead of the warm
        kitchen proxy -- the fix for the reported drift (20C room, window open -> the old
        dashboard still said 'deploy AC to ~23').

        e_active is the weather model's DAYTIME apartment peak. Using it directly as the
        overnight equilibrium is only valid on a HOT night; on a cool/cooling night the
        sealed room drifts toward the cool NIGHT apartment, not the daytime peak, so the plan
        would recommend cooling a flat that's already below its limit and getting colder
        (2026-07-22: every room ~21.7C, outdoor 17->~15C, yet the plan said 'run the AC ->
        peak 24.6C'). cm.grounded_equilibrium reality-checks e_active against apartment_now +
        a margin unless the night stays warm enough to hold the day's heat -- see that fn.
        night_outdoor (tonight's forecast low) is ALSO passed into the plan as
        SleepPlanInputs.night_outdoor (2026-07-29): plan_sleep judges window feasibility
        against it instead of the live outdoor reading, which is the day's minimum at 05:30
        and made a window look sufficient every single morning -- see plan_sleep's docstring.

        Pricing is deficit-sized (2026-07-29): the estimate blends the price over the slots
        the job will ACTUALLY occupy (k = ceil(minutes_needed / 15), from the SAME
        target/deficit math plan_sleep itself runs on the same inputs), not a fixed
        est_price_slots or the single cheapest slot -- real blended price paid across 15
        metered days (2026-07-10..29) was 1.39-1.52 kr/kWh vs the ~0.5 kr/kWh the old
        single-cheapest-slot code produced. est_price_slots remains the floor/fallback k for
        when the deficit can't be computed yet (missing floor or equilibrium)."""
        try:
            t_in = await self._num(self.comfort_temp_entity, None)
            rh_in = await self._num(self.comfort_rh_entity, None)
            t_out = await self._num(self.outdoor_sensor, None)
            rh_out = await self._num(self.outdoor_rh_entity, None)
            indoor_dew = cm.dew_point_c(t_in, rh_in)
            outdoor_dew = cm.dew_point_c(t_out, rh_out)
            contacts = {}
            for name, ent in self.window_contact_entities.items():
                contacts[name] = await self._state(ent)
            open_windows = cm.summarize_open_windows(contacts)
            pm = self._build_price_map(
                await self._attr(self.price_entity, "raw_today", []),
                await self._attr(self.price_entity, "raw_tomorrow", []),
            )
            price_now = self._price_for(pm, now, await self._num(self.price_entity, 1.7))
            ceiling, _ = await self._effective_ceiling(now)
            # Bedroom-zone reality anchor (the A/C is bedroom-only and the bedroom is its own
            # thermal zone -- user 2026-07-22). The sealed room can't drift materially warmer
            # than ITS OWN zone is right now on a cool night: its floor (passed in) + mid wall +
            # the kitchen wall it conducts against. NOT the living/dining rooms. Warmest of the
            # three (errs deep -> safe).
            zone_readings = [floor,
                             await self._num(self.mid_sensor, None),
                             await self._num(self.kitchen_sensor, None)]
            zone_vals = [v for v in zone_readings if v is not None]
            bedroom_zone_now = max(zone_vals) if zone_vals else None
            night_outdoor = await self._night_outdoor_min(now)
            plan_equilibrium = cm.grounded_equilibrium(
                e_active, bedroom_zone_now, night_outdoor, ceiling,
                self.wm_reality_margin, self.wm_warm_night_margin)
            grounded = (plan_equilibrium is not None and e_active is not None
                        and plan_equilibrium < e_active - 0.05)
            # Deficit-sized pricing (see the docstring): k is the number of 15-min slots the
            # job actually needs, from the same calc_floor_target/deficit math plan_sleep
            # itself runs on the same inputs -- est_price_slots is only the fallback k for
            # when the deficit can't be computed yet (missing floor or equilibrium).
            # Price the job the unit can ACTUALLY do: cap the advisory's depth at the learned
            # feasible floor exactly as _reach_target caps the armed path. Without this the
            # plan sized (and charged for) a run down to min_temp 16C while the floor has
            # proven it stops around 20.5 -- 2026-07-29 that phantom ~4C showed up as
            # "Run the AC ~12.5 kr" against a real ~5 kr job the controller was already
            # running to 20.2C.
            plan_min = self._plan_floor_limit()
            if plan_equilibrium is not None and floor is not None:
                pricing_target = cm.calc_floor_target(plan_equilibrium, ceiling,
                                                      self._rise_frac, self.zone_offset,
                                                      plan_min)
                pricing_deficit = max(0.0, floor - pricing_target)
                minutes_needed = self._cooling_minutes(floor, pricing_target)
                price_slots = max(1, math.ceil(minutes_needed / 15.0))
            else:
                price_slots = self.est_price_slots
            cheapest = self._blended_cheap(pm, now, self._deadline(now), price_now, price_slots)
            plan = cm.plan_sleep(cm.SleepPlanInputs(
                floor=floor, equilibrium=plan_equilibrium, rise_frac=self._rise_frac,
                zone_offset=self.zone_offset, comfort_limit=ceiling, min_temp=plan_min,
                floor_cool_cph=self._cool_rate(), cool_power_kw=self.cool_kw,
                cheapest_price=cheapest, outdoor_temp=t_out, outdoor_dew=outdoor_dew,
                indoor_dew=indoor_dew, open_windows=open_windows,
                noise_penalty_kr=self.ac_noise_penalty_kr,
                night_outdoor=night_outdoor, kwh_per_deg=self._kwh_per_deg))
            # The evening rescue's single source of truth: it DELIVERS this plan's verdict
            # instead of recomputing its own projection (2026-07-23: the rescue still used
            # the un-grounded kitchen proxy and pushed "deploy the AC" at a 21.6C bedroom
            # while this very plan said windows -- one brain, two delivery moments). deficit
            # is ALSO the anchor _track_session_cost captures at session start for
            # kwh_per_deg learning (see _finalize_session).
            self._last_plan = {"rec": plan["recommendation"],
                               "equilibrium": plan_equilibrium,
                               "limit": ceiling,
                               "peak": plan.get("projected_peak"),
                               "deficit": plan.get("deficit")}
            detail = plan["detail"]
            if grounded:
                detail += (f" (Grounded on reality: the bedroom zone is ~{bedroom_zone_now:.1f}C "
                           f"now and tonight's low is ~{night_outdoor:.1f}C, so the sealed room "
                           f"drifts toward that, not the daytime peak {e_active:.1f}C.)")
            # Wake display for the Tonight card: WHEN you wake (alarm else day-type
            # fallback via cm.resolve_wake) and what you'll wake TO. On an AC/hybrid plan
            # the promise assumes the pre-cool lands (coast from the priced target);
            # otherwise it's the plan's own no-AC coast peak. Display only.
            alarm_t = await self._state(self.alarm_time_entity)
            alarm_on = (await self._state(self.alarm_enabled_entity)) == "on"
            wake_dt = cm.resolve_wake(now, alarm_t, alarm_on,
                                      self.fallback_workday, self.fallback_weekend)
            wake_proj = plan.get("projected_peak")
            if (plan["recommendation"] in ("ac", "hybrid")
                    and plan_equilibrium is not None and floor is not None):
                wp = cm.coast_peak(pricing_target, plan_equilibrium,
                                   self._rise_frac, self.zone_offset)
                if wp is not None:
                    wake_proj = round(min(wp, plan.get("projected_peak") or wp), 1)
            # One voice: the card renders the same verdict the push composes.
            v_deployed = (await self._state(self.climate_entity)) not in (
                None, "unavailable", "unknown")
            v_armed = (await self._state(self.enable_entity)) == "on"
            v_title, v_text = cm.compose_briefing(
                plan["recommendation"], plan, {}, v_deployed, v_armed)
            # Load-bearing strings (cost_label/windows_summary) never rely on a 0/False/None
            # value that AppDaemon 4.5.13 would drop; est_cost_kr==0 legitimately vanishes and
            # the dashboard reads cost_label instead. open_windows stays a list ([] survives).
            attrs = {
                "friendly_name": "Sleep plan",
                "icon": "mdi:bed-clock",
                "recommendation": plan["recommendation"],
                "headline": plan["headline"],
                "detail": detail,
                "reason": detail,
                "comfort_limit": plan["comfort_limit"],
                "est_cost_kr": plan["est_cost_kr"],
                "cost_label": plan["cost_label"],
                "open_windows": plan["open_windows"],
                "windows_summary": plan["windows_summary"],
                "source_entities": [self.floor_sensor, self.mid_sensor, self.kitchen_sensor,
                                    self.comfort_temp_entity,
                                    self.comfort_rh_entity, self.outdoor_sensor,
                                    self.outdoor_rh_entity, self.price_entity,
                                    self.weather_forecast_entity, self.ac_energy_entity,
                                    *self.window_contact_entities.values()],
                "computed_at": now.isoformat(timespec="seconds"),
                # Transparency: the raw weather peak, the reality anchors, and the grounded
                # value actually projected. All non-zero floats/strings so AppDaemon 4.5.13's
                # False/0/None drop can't silently strip a load-bearing one; grounded is a
                # STRING ("true"/"false") for the same reason (a raw False would vanish).
                "grounded": "true" if grounded else "false",
            }
            if plan["projected_peak"] is not None:
                attrs["projected_peak"] = plan["projected_peak"]
            if e_active is not None:
                attrs["equilibrium_weather"] = round(e_active, 1)
            if plan_equilibrium is not None:
                attrs["equilibrium_planned"] = round(plan_equilibrium, 1)
            if bedroom_zone_now is not None:
                attrs["bedroom_zone_now"] = round(bedroom_zone_now, 1)
            if night_outdoor is not None:
                attrs["night_outdoor_min"] = round(night_outdoor, 1)
            if wake_dt is not None:
                attrs["wake_at"] = wake_dt.strftime("%H:%M")
                # The moment the plan actually protects from: wake minus the sleep window.
                # Published so the dashboard's bedtime boundary is THIS number, never a
                # card-side constant (user 2026-07-30: bar and schedule disagreed on it).
                attrs["bedtime_at"] = (wake_dt - timedelta(hours=self.sleep_hours)).strftime("%H:%M")
            if wake_proj is not None:
                attrs["wake_projection"] = wake_proj
            if v_title:
                attrs["verdict_title"] = v_title
            if v_text:
                attrs["verdict_text"] = v_text
            await self.set_state(self.sleep_plan_entity, state=plan["recommendation"],
                                 replace=True, attributes=attrs)
        except Exception as e:
            self.log(f"sleep-plan publish failed ({e}) -- skipping", level="WARNING")

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
        # Measure + model EVERY tick, regardless of arm/deploy state (read-only, NEVER
        # actuates): the kitchen peak, the legacy proxy and the weather equilibrium are the
        # data the shadow model most needs on exactly the cool/disarmed days that used to
        # record nothing (the whole compute lived below the arm guard). Actuation still
        # happens only in the armed+deployed path further down; here we only gather + publish.
        # mid defaults to None (folded to floor in the armed path so zone/attrs are byte-
        # identical); _equilibrium already ignores None, and floor is a member of the max
        # either way, so e_legacy matches the old armed-path read exactly.
        kitchen = await self._num(self.kitchen_sensor, None)
        mid = await self._num(self.mid_sensor, None)
        floor = await self._num(self.floor_sensor, None)
        self._track_kitchen_max(now, kitchen)   # running daily max + midnight rollover
        # Live session-cost metering (see the ac_energy_entity config comment): every tick,
        # armed or not, so a flapping deploy never truncates a session. price_now here is the
        # price sensor's own STATE (current kr/kWh) -- a plain, cheap read, independent of the
        # armed path's price-map/fallback logic further below (which stays byte-identical).
        energy_counter = await self._num(self.ac_energy_entity, None)
        price_now_cheap = await self._num(self.price_entity, None)
        self._track_session_cost(deployed, price_now_cheap, energy_counter)
        e_legacy = self._equilibrium(kitchen, mid, floor)
        e_active, wm_dbg = await self._weather_equilibrium(now, kitchen, mid, floor, e_legacy)
        await self._check_deploy_watchdog(now, master_on, deployed)
        # Advisory sleep plan (windows vs AC) -- read-only, command-free, every branch.
        # Placed BEFORE the arm gate so it can never feed back into actuation; projects from
        # e_active (weather E) not the warm kitchen proxy. Self-contained try/except inside.
        await self._publish_sleep_plan(now, floor, e_active)

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
                    {"deployed": deployed, "bathroom": round(bath, 1),
                     **self._status_base(), **wm_dbg})
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
            # Fallback session close-out: the user unplugged the AC without pressing "AC
            # removed" first (the normal close-out lives in that press's branch above), so
            # a session left open would otherwise meter forever. Only fires disarmed+
            # undeployed -- exactly the state a genuinely-ended night settles into.
            if not deployed and self._session_kwh0 is not None:
                self._finalize_session()
                self._finalize_night()
            # Evening rescue advisory (never a command). Placed after the hazard handling so
            # a genuine safety-off doesn't also nag; it only reads/notifies.
            await self._maybe_evening_rescue(now, floor)
            # OFF = HANDS OFF. Turn the AC off ONCE on the on->off flip, then never command it again.
            if self._master_was_on:
                await self._ensure_off("off", "Disarmed -- AC turned off, now hands-off",
                                       {"deployed": deployed, **self._status_base(), **wm_dbg})
            else:
                await self._publish("off", "Disarmed -- hands off (manual AC control)",
                                    {"deployed": deployed, **self._status_base(), **wm_dbg})
            self._master_was_on = False
            return
        self._master_was_on = True
        if not deployed:
            self._mark_eval(now, False)
            await self._publish("unit_stored", "AC not deployed (climate unavailable)",
                                {**self._status_base(), **wm_dbg})
            return

        # floor/mid/kitchen were read (hoisted) above; preserve the armed no_data guard.
        if floor is None:
            self._mark_eval(now, False)
            await self._publish("no_data", "Missing bedroom floor temperature",
                                {**self._status_base(), **wm_dbg})
            return
        if mid is None:
            mid = floor   # armed-path default: mid tracks floor when its own sensor is dark
        ceil_s = await self._num(self.ceiling_sensor, None)
        ac_s = await self._num(self.ac_sensor, None)

        # Comfort layer may lower the ceiling on humid/two-sleeper nights (bounded; see
        # _effective_ceiling). The disarmed rescue check shares the same helper.
        ceiling, ceiling_base = await self._effective_ceiling(now)
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
        # computed EVERY tick (hoisted above the arm guard) but only DRIVES actuation once
        # it's enabled and out of shadow. While wm_shadow is true, E == e_legacy exactly
        # (zero actuation change) and the weather value rides along on the status entity for
        # predicted-vs-actual validation. e_legacy/e_active/wm_dbg were computed above.
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
        # Learn how fast the floor ACTUALLY drops while engaged (see _track_cool_rate);
        # everything downstream sizes its minutes off _cool_rate(), not the seed.
        self._track_cool_rate(floor, engaged, self._last_want)
        # Tonight's achieved depth + engaged time -> _finalize_night learns from it.
        if engaged > 0 and floor is not None:
            self._night_engaged_min += engaged
            self._night_floor_min = (floor if self._night_floor_min is None
                                     else min(self._night_floor_min, floor))
        reach_target = self._reach_target(now, target, saturated)
        reach_deficit = floor - reach_target
        minutes_needed = self._cooling_minutes(floor, reach_target)

        # user says they're removing the AC now -> this IS lights-out: stash the coast baseline,
        # graceful compressor stop (they unplug right after -- better than yanking power
        # mid-run), then reset the toggle so it's a one-shot trigger.
        if (await self._state(self.ac_removed_entity)) == "on":
            self._mark_eval(now, False)
            self._stash_lightout(floor, E, now)
            self._finalize_session()   # AC removed -- close out tonight's metered session
            self._finalize_night()     # ...and learn how deep the room actually got
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

        cool_now, next_start, run_min, est_cost, plan_windows = self._schedule(
            now, deadline, minutes_needed, price_at, already_cooling=self._last_want)
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
                            ceiling_base, reach_target=reach_target, wm_dbg=wm_dbg,
                            plan_windows=plan_windows)

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

    def _status_base(self):
        """Learned attributes that must ride on EVERY status publish, armed or not. The
        armed path folds these into _attrs, but the disarmed / not-deployed / no_data
        branches publish with replace=True, which would otherwise WIPE rise_frac from
        sensor.smart_cooling_status on those (daytime-dominant) ticks -- bedroom_comfort
        reads it back as a display passthrough and would silently fall to its 0.5 fallback
        despite a learned ~0.7. Same value the armed _attrs publishes, so the two never
        disagree. rise_frac is a non-zero float so it survives AppDaemon 4.5.13's
        False/0/None attribute-drop; rise_samples can be 0 (dropped then -- display only,
        harmless).

        Also carries the live/learned session-cost numbers (see _track_session_cost /
        _finalize_session): last_night_cost_kr / night_cost_ema_kr once a night's been
        metered, and session_cost_kr / session_kwh while a session is open or has anything
        to show. Each is included only when meaningful -- AppDaemon 4.5.13 silently drops a
        False/0/None attribute anyway, so publishing a raw 0.0 here would be misleading
        (looks like "metered, cost 0" rather than "nothing metered yet").

        kwh_per_deg rides unconditionally like rise_frac -- it always has a non-zero value
        (the seeded default or a real EMA, never legitimately 0) so the attribute-drop bug
        never bites it; kwh_per_deg_samples can be 0 (dropped then -- display only, same as
        rise_samples)."""
        out = {"rise_frac": round(self._rise_frac, 2), "rise_samples": self._rise_samples,
               "kwh_per_deg": round(self._kwh_per_deg, 3),
               "kwh_per_deg_samples": self._kwh_per_deg_samples,
               # planning rate: learned C/h once sampled, else the seed (never 0)
               "cool_cph": round(self._cool_rate(), 2),
               "cool_cph_samples": self._cool_cph_samples}
        if self._last_session_cost is not None:
            out["last_night_cost_kr"] = self._last_session_cost
        if self._last_session_kwh:
            out["last_night_kwh"] = self._last_session_kwh
        if self._last_wake_floor is not None:
            out["last_night_wake_temp"] = self._last_wake_floor
        if self._feasible_floor is not None:
            out["feasible_floor"] = self._feasible_floor
        if self._night_cost_ema is not None:
            out["night_cost_ema_kr"] = self._night_cost_ema
        if self._session_kwh0 is not None or self._session_cost > 0:
            out["session_cost_kr"] = round(self._session_cost, 2)
            out["session_kwh"] = round(self._session_kwh, 2)
        # Plug-in moment of the open session -- the dashboard bar opens here ("on duty"
        # starts at the plug, not at the first compressor start; user 2026-07-31).
        if self._session_started_at is not None:
            out["session_started_at"] = self._session_started_at
        return out

    def _attrs(self, floor, mid, zone, ceil_s, ac_s, bath, kitchen, E, target, deficit,
               ceiling, price_now, window_open, run_min, next_start, est_cost, floor_limited,
               ceiling_base, reach_target=None, wm_dbg=None, plan_windows=()):
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
            # The scheduler's actual chosen stretches (ISO pairs, no Nones -- safe from the
            # 4.5.13 publish strip). The card draws these instead of projecting now+minutes.
            "planned_cool_windows": [[w[0].isoformat(), w[1].isoformat()] for w in plan_windows],
            "est_cost_kr": est_cost, "dry_run": self.dry_run,
            "last_burp": self._last_burp.strftime("%H:%M") if self._last_burp else None,
            "dry_min_tonight": round(self._dry_min),
        }
        # _status_base()'s learned/session-cost keys ride on the armed publish too (e.g. a
        # session actively metering while cooling) -- merged here rather than duplicated;
        # its rise_frac/rise_samples recompute to the exact values already set above, so
        # this is a no-op for those two and additive for the rest.
        out.update(self._status_base())
        # Weather-model shadow attributes (equilibrium_weather/legacy, solar/outdoor/kitchen
        # estimates) for predicted-vs-actual comparison on the status entity. wm_shadow is
        # published as a STRING to survive AppDaemon's False/0/None attribute-drop (see the
        # _publish comment / window_open pattern) -- a raw False would vanish from the entity.
        if wm_dbg:
            out.update(wm_dbg)
        out["wm_shadow"] = "true" if self.wm_shadow else "false"
        # Forecast staleness (see _get_forecast/_fc_served_age_min): omitted when nothing
        # has been served yet (mirrors the equilibrium_weather/bedroom_zone_now "only when
        # meaningful" pattern above). Published as a float so a genuinely-fresh 0 survives
        # AppDaemon 4.5.13's False/0/None attribute-drop (the bug hits int 0/False).
        if self._fc_served_age_min is not None:
            out["wm_forecast_age_min"] = float(self._fc_served_age_min)
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
    # bathroom window or a stop-start evening cannot flood the feed. cool_on's 20h window
    # means one entry per day's session while tomorrow's still reports -- at 4h every
    # pre-cool resume after a price hold re-reported (2026-07-22: three "cooling to 16C"
    # entries in one day).
    FEED_COOLDOWN_MIN = {"cool_on": 1200, "off_window": 60, "off_hardcap": 120,
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

    @staticmethod
    def _feed_cause(reason, limit=110):
        """Feed copy of a planner reason: the feed hard-slices cause at 120 chars, which
        showed mid-word cuts like "[capped by fea". Drop the card-facing "  [...]" fine
        print and end on a word boundary; the card/log keep the full string."""
        cause = reason.split(" [")[0].rstrip()
        if len(cause) > limit:
            cut = cause[:limit]
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            cause = cut.rstrip(" ,;:-.") + "..."
        return cause

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
                # reason is the planner's own explanation of WHY (cheap hour, deadline,
                # deficit...); the feed gets a trimmed copy, the card/log keep it whole
                await self._report_house_event(
                    "cool_on", self._feed_cause(reason),
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
    # Day-history for the dashboard bar (2026-07-30): the dashboard's own definition of
    # "actively cooling" -- must match this exact pair (dry_run's "cooling_dryrun" is
    # deliberately NOT included; the day bar is a real-run history, not a rehearsal one).
    ENGAGED_STATUSES = {"cooling", "burping"}

    def _track_cool_intervals(self, status, now):
        """Update self._cool_intervals from a status TRANSITION, compared against the
        previously PUBLISHED status (_last_pub_status -- what the entity actually said,
        not the internal want/decision), so a re-publish of the same status is a no-op.
        On entering ENGAGED_STATUSES with nothing already open, append [now_iso, None];
        whenever the current status ISN'T engaged and an interval is left open, close it
        with now_iso -- this single check covers both the ordinary transition-out AND the
        self-heal case (an app/AppDaemon restart mid-run: _last_pub_status resets to None,
        so the next non-engaged tick wouldn't otherwise read as a was-engaged->false
        transition, but a reloaded _cool_intervals can still have that run left open, and
        an OPEN interval is exempt from the 24h prune below so left alone it would linger
        on the dashboard bar forever). Symmetrically, never stack a second open interval on
        top of one already open (e.g. that same restart, landing on a still-engaged tick).
        now must be tz-aware (see _publish) -- published intervals carry a UTC offset."""
        now_iso = now.isoformat()
        is_engaged = status in self.ENGAGED_STATUSES
        was_engaged = self._last_pub_status in self.ENGAGED_STATUSES
        has_open = bool(self._cool_intervals) and self._cool_intervals[-1][1] is None
        changed = False
        if is_engaged and not was_engaged and not has_open:
            self._cool_intervals.append([now_iso, None])
            changed = True
        elif not is_engaged and has_open:
            self._cool_intervals[-1][1] = now_iso
            changed = True
        before = len(self._cool_intervals)
        self._prune_cool_intervals(now)
        changed = changed or len(self._cool_intervals) != before
        self._last_pub_status = status
        if changed:
            self._save_state()

    def _prune_cool_intervals(self, now):
        """Drop CLOSED intervals whose end is >24h old -- the dashboard bar only spans
        ~10:00 today through wake+1h tomorrow, so nothing older is ever drawn. An open
        interval (end None) is always kept regardless of how old its start is. Defensive
        against an unparseable end (shouldn't happen post-_valid_cool_interval, but
        pruning must never be what crashes the eval loop): dropped rather than kept."""
        cutoff = now - timedelta(hours=24)
        kept = []
        for iv in self._cool_intervals:
            end = iv[1]
            if end is None:
                kept.append(iv)
                continue
            try:
                if datetime.fromisoformat(end) >= cutoff:
                    kept.append(iv)
            except (TypeError, ValueError):
                continue
        self._cool_intervals = kept

    async def _publish(self, status, reason, attrs):
        a = dict(attrs or {})
        a["reason"] = reason
        a["friendly_name"] = "Smart cooling status"
        a["icon"] = "mdi:snowflake-thermometer"
        now = await self.get_now()
        self._track_cool_intervals(status, now)
        a["cool_intervals_today"] = list(self._cool_intervals)
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
