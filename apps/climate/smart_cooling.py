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
    off at 23:52 mid-deficit, hence the 1h floor).

Stall-breaker (2026-07-15): the unit regulates off its own intake sensor, which sits in its cold
outflow pool -- it parks at ~300 W "idle" with the floor still degrees above target, and no fan
mode fixes that (low/high/full all measured stalled). When a tick sees idle + real deficit, a
short fan_only burp lets the intake read true room air and the compressor restarts hard.

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
slots and let venting happen in hours we'd hold anyway; only near-derate heat (hard cap) stops us.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime, timedelta
import json
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
        self.outdoor_sensor = a("outdoor_temp_sensor", "sensor.gw2000a_outdoor_temperature")    # minor (sealed/blackout)
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
        # Condenser-room temps: above `bathroom_max` the condenser loses ~2-3%/C efficiency
        # (worth flagging, NOT worth skipping a cheap slot for -- door is kept sealed, so no
        # real back-leak; user 2026-07-16). Above `bathroom_hard_max` we stop regardless of
        # price: derate territory, extra watts stop moving heat.
        self.bathroom_max = float(a("bathroom_backleak_c", 30.0))
        self.bathroom_hard_max = float(a("bathroom_hard_max_c", 33.0))
        # --- actuation. Fan draw is trivial (44 W even at full, measured 2026-07-15 sweep;
        # the compressor is ~500-800 W) and NO fan mode prevents the intake stall below, so
        # medium is kept purely for air distribution. Low setpoint so it doesn't idle a
        # degree early. ---
        self.cool_setpoint = float(a("cool_setpoint", 17.0))
        self.cool_fan = a("cool_fan_mode", "medium")
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
        self._was_drying = False                   # previous eval ran the dry-finish
        self._dry_date = None                      # calendar day the dry budget belongs to
        self._dry_min = 0.0                        # dry-finish minutes spent that day
        # learned warm-up indicator (+ the in-flight coast record) + learned feasible floor
        self._rise_frac = self.default_rise_frac
        self._rise_samples = 0
        self._lightout: Optional[dict] = None
        self._feasible_floor: Optional[float] = None
        self._feasible_samples = 0
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
        genuinely closed window pushes the unvented condenser room past bathroom_hard_max
        within minutes, and that guard stops cooling regardless of what the contact says."""
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
        maintenance horizon: hold the floor at target, at the going price, until the
        AC-removed press (or 06:00, when normal next-night planning resumes)."""
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
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"rise_frac": self._rise_frac, "rise_samples": self._rise_samples,
                           "lightout": self._lightout,
                           "feasible_floor": self._feasible_floor,
                           "feasible_samples": self._feasible_samples}, f)
        except Exception as e:
            self.log(f"state save failed ({e}) -- continuing in-memory", level="WARNING")

    def _equilibrium(self, kitchen, mid, floor):
        """Where the sealed sleeping zone drifts overnight. Driven by the neighbour wall (kitchen)
        and the room's own warm baseline + the sleeper. Conservative: take the warmest sensible
        reading (errs deep -> safe)."""
        vals = [v for v in (kitchen, mid, floor) if v is not None]
        return (max(vals) if vals else 24.5) + self.person_offset

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

    # ---------- main ----------
    async def _evaluate(self):
        now = (await self.get_now()).replace(tzinfo=None)
        await self._learn(now)   # read-only; runs regardless of arm/deploy

        master_on = (await self._state(self.enable_entity)) == "on"
        climate_state = await self._state(self.climate_entity)
        deployed = climate_state not in (None, "unavailable", "unknown")

        if not master_on:
            # OFF = HANDS OFF. Turn the AC off ONCE on the on->off flip, then never command it again.
            self._mark_eval(now, False)
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
        bath = await self._num(self.bathroom_sensor, None)
        kitchen = await self._num(self.kitchen_sensor, None)

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
        E = self._equilibrium(kitchen, mid, floor)
        target = self._calc_target(E, ceiling)
        deficit = floor - target
        floor_limited = target <= self.min_temp + 0.05  # hot night: cooling as deep as the unit allows

        # Feasibility cap: schedule (and pay) only for depth the floor will actually take.
        # `target` stays the IDEAL for display/learning; `reach_target` is what we pursue.
        # Tonight's live evidence (saturated at _sat_min) beats the cross-night learned
        # limit; the learned limit is probed 0.3C deep so a milder night can beat it and
        # re-teach us, instead of the clamp becoming self-fulfilling.
        engaged = 0.0
        if self._last_want and self._last_eval_at is not None:
            engaged = min((now - self._last_eval_at).total_seconds() / 60.0,
                          self.interval_min * 1.5)
        saturated = self._track_progress(floor, engaged)
        reach_target = target
        if saturated and self._sat_min is not None:
            reach_target = max(reach_target, self._sat_min)
        elif (self._feasible_floor is not None
              and self._feasible_samples >= self.feasible_min_samples):
            reach_target = max(reach_target, self._feasible_floor - 0.3)
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
                            ceiling_base),
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
        # in hours we'd hold anyway. Only the hard cap stops cooling: near condenser-derate
        # territory more watts stop buying heat-moved at any price.
        backleak_hard = bath is not None and bath >= self.bathroom_hard_max
        bath_warm = bath is not None and bath >= self.bathroom_max

        # decision
        if floor <= self.min_temp:
            want, reason = False, f"Floor at min ({floor:.1f}<= {self.min_temp:.1f}) -- holding"
        elif deficit <= 0.05:
            want, reason = False, f"On track: floor {floor:.1f}C <= target {target:.1f}C (zone {zone:.1f}, cap {ceiling:.0f})"
        elif reach_deficit <= 0.05:
            lim = self._sat_min if saturated else self._feasible_floor
            want, reason = False, (f"As cold as it feasibly gets: floor {floor:.1f}C, ideal "
                                   f"{target:.1f}C, but the floor stops taking cold around "
                                   f"{lim:.1f}C -- holding rather than paying for cooling it "
                                   f"won't absorb")
        elif not window_open:
            want, reason = False, "Bathroom window closed -- open it so the condenser can vent"
        elif backleak_hard:
            want, reason = False, (f"Bathroom {bath:.1f}C past the {self.bathroom_hard_max:.0f}C hard cap "
                                   f"-- venting before the condenser derates")
        elif cool_now:
            want = True
            reason = (f"Pre-cool floor {floor:.1f}->{reach_target:.1f}C (keep zone <= {ceiling:.0f} for "
                      f"{self.sleep_hours:.0f}h): ~{run_min} min in the cheapest hours, ~{est_cost:.1f} kr, "
                      f"price {price_now:.2f}"
                      + ("  [floor-limited: hottest it can do]" if floor_limited else "")
                      + (f"  [capped by feasible ~{reach_target:.1f}C, ideal {target:.1f}C]"
                         if reach_target > target + 0.05 else "")
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
                            ceiling_base, reach_target=reach_target)

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
               ceiling_base, reach_target=None):
        def r1(v):
            return round(v, 1) if v is not None else None
        return {
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
        if "hard cap" in reason:
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
    async def _apply_cool(self, reason, status, attrs, deficit, now):
        cur_mode = await self._state(self.climate_entity)
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
                if await self._bed_occupied():
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
            self.log(f"DRY-RUN would COOL ({self.cool_setpoint}C/{self.cool_fan}): {reason}")
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
                self.log(f"COOL on ({self.cool_setpoint}C/{self.cool_fan}): {reason}", level="INFO")
                # reason is the planner's own explanation of WHY (cheap hour, deadline, deficit...)
                await self._report_house_event(
                    "cool_on", reason,
                    f"AC cooling the bedroom to {self.cool_setpoint:g}C", now)
            cur_temp = await self._attr(self.climate_entity, "temperature", None)
            if cur_temp is None or abs(float(cur_temp) - self.cool_setpoint) > 0.1:
                await self.call_service("climate/set_temperature", entity_id=self.climate_entity, temperature=self.cool_setpoint)
            cur_fan = await self._attr(self.climate_entity, "fan_mode", None)
            if cur_fan != self.cool_fan:
                try:
                    await self.call_service("climate/set_fan_mode", entity_id=self.climate_entity, fan_mode=self.cool_fan)
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
