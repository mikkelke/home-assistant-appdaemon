"""
Smart Cooling v2 -- closed-loop pre-cool for the bedroom PortaSplit AC.

Opt-in: Arm = input_boolean.smart_cooling, and the unit must be deployed (climate available).
The job: gently pre-cool the FURNITURE/floor "battery" before bedtime, in the cheapest hours,
deep enough that the sealed room (AC removed) coasts with the floor-to-mid SLEEPING ZONE
<= the ceiling (default 23C) for ~8 h.

Closed-loop, not predictive: the FLOOR sensor is the feedback. Each tick it compares the floor to a
CALCULATED target (no knob) and schedules the remaining cooling into the cheapest hours, re-deciding
every tick so drift self-corrects. The target comes from a self-learning warm-up indicator -- every
sealed sleep window IS an 8 h AC-off coast, so we learn "from floor F0 the zone closed fraction r of
the gap to its equilibrium" and invert it.

Knobs you ever touch: Arm + Bedtime. Ceiling defaults to 23C; depth is calculated. Master OFF =
hands-off. dry_run makes every AC command a no-op. The bathroom (condenser dump, leaky door) is
watched so we ease off rather than back-leak heat into the bedroom.
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
        # --- the only user knobs: Arm (above) + Bedtime; ceiling is an optional default ---
        self.bedtime_entity = a("bedtime_entity", "input_datetime.smart_cooling_bedtime")
        self.night_ceiling_entity = a("night_ceiling_entity", "input_number.smart_cooling_night_ceiling")
        # Humidity-aware ceiling from the comfort middle layer (sensor.bedroom_comfort).
        self.comfort_entity = a("comfort_entity", "sensor.bedroom_comfort")
        self.comfort_max_reduction = float(a("comfort_max_reduction", 1.5))
        # --- fixed params (not user-facing) ---
        self.default_bedtime = str(a("default_bedtime", "23:00"))
        self.default_ceiling = float(a("default_night_ceiling", 23.0))
        self.min_temp = float(a("min_temp", 16.0))            # hardware floor; never drive below
        self.sleep_hours = float(a("sleep_hours", 8.0))
        self.floor_cool_cph = float(a("floor_cool_cph", 1.0))     # measured ~1 C/h floor/mass cool rate
        self.zone_offset = float(a("zone_offset", 1.0))           # mid wall sits ~this above the floor
        self.person_offset = float(a("person_offset", 0.5))      # sleeper lifts the equilibrium a touch
        self.default_rise_frac = float(a("default_rise_frac", 0.7))  # conservative gap-fraction closed in the window
        self.bathroom_max = float(a("bathroom_backleak_c", 30.0))    # ease off above this (condenser back-leak)
        # --- actuation (GENTLE: low fan = higher COP, quieter, less back-leak; low setpoint so it doesn't idle) ---
        self.cool_setpoint = float(a("cool_setpoint", 17.0))
        self.cool_fan = a("cool_fan_mode", "medium")
        self.cool_kw = float(a("cool_power_kw", 0.6))   # gentle draw, est-cost only
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
        self._notified_bedtime_for: Optional[str] = None
        self._last_reason: Optional[str] = None
        # learned warm-up indicator (+ the in-flight coast record)
        self._rise_frac = self.default_rise_frac
        self._rise_samples = 0
        self._lightout: Optional[dict] = None
        self._load_state()

        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")

        for ent in (self.enable_entity, self.price_entity, self.bedtime_entity,
                    self.night_ceiling_entity, self.vent_window):
            self.listen_state(self._on_trigger, ent)
        self.run_every(self._run_eval, "now", self.interval_min * 60)
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

    async def _bedtime_dt(self, now):
        s = await self._state(self.bedtime_entity)
        hhmm = str(s) if s and ":" in str(s) else self.default_bedtime
        try:
            p = hhmm.split(":")
            h, m = int(p[0]), int(p[1])
        except (ValueError, IndexError):
            h, m = 23, 0
        return now.replace(hour=h, minute=m, second=0, microsecond=0)

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
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"rise_frac": self._rise_frac, "rise_samples": self._rise_samples,
                           "lightout": self._lightout}, f)
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

    def _schedule(self, now, bedtime, minutes_needed, price_at):
        """Reserve the cheapest `minutes_needed` of 15-min slots between now and bedtime. Cool NOW if
        the current slot is one of them, or if there isn't time left to wait. Returns
        (cool_now, next_start, run_min, est_cost)."""
        total = int((bedtime - now).total_seconds() // 900)
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
        """Runs every tick (any arm/deploy state, read-only): stash the floor at bedtime, then learn
        the gap-fraction the zone closed once the 8 h sealed window finishes."""
        floor = await self._num(self.floor_sensor, None)
        if floor is None:
            return
        lo = self._lightout
        # record a finished window
        if lo:
            try:
                ended = now >= datetime.fromisoformat(lo["end"])
            except Exception:
                self._lightout = None
                self._save_state()
                return
            if ended:
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
                lo = None
        # stash a fresh lights-out at bedtime
        bedtime = await self._bedtime_dt(now)
        today = now.strftime("%Y-%m-%d")
        if (not lo or lo.get("date") != today) and bedtime <= now < bedtime + timedelta(minutes=20):
            mid = await self._num(self.mid_sensor, floor)
            kitchen = await self._num(self.kitchen_sensor, None)
            E = self._equilibrium(kitchen, mid, floor)
            self._lightout = {"date": today, "F0": round(floor, 2), "E": round(E, 2),
                              "end": (bedtime + timedelta(hours=self.sleep_hours)).isoformat()}
            self._save_state()
            self.log(f"Lights-out {today}: floor {floor:.1f}C, equilibrium {E:.1f}C "
                     f"-> learning the rise at {(bedtime + timedelta(hours=self.sleep_hours)).strftime('%H:%M')}",
                     level="INFO")

    # ---------- main ----------
    async def _evaluate(self):
        now = (await self.get_now()).replace(tzinfo=None)
        await self._learn(now)   # read-only; runs regardless of arm/deploy

        master_on = (await self._state(self.enable_entity)) == "on"
        climate_state = await self._state(self.climate_entity)
        deployed = climate_state not in (None, "unavailable", "unknown")

        if not master_on:
            # OFF = HANDS OFF. Turn the AC off ONCE on the on->off flip, then never command it again.
            if self._master_was_on:
                await self._ensure_off("off", "Disarmed -- AC turned off, now hands-off", {"deployed": deployed})
            else:
                await self._publish("off", "Disarmed -- hands off (manual AC control)", {"deployed": deployed})
            self._master_was_on = False
            return
        self._master_was_on = True
        if not deployed:
            await self._publish("unit_stored", "AC not deployed (climate unavailable)", {})
            return

        floor = await self._num(self.floor_sensor, None)
        if floor is None:
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
        bedtime = await self._bedtime_dt(now)

        pm = self._build_price_map(
            await self._attr(self.price_entity, "raw_today", []),
            await self._attr(self.price_entity, "raw_tomorrow", []),
        )
        price_now = self._price_for(pm, now, await self._num(self.price_entity, 1.7))
        price_at = lambda dt: self._price_for(pm, dt, price_now)
        window_open = (await self._state(self.vent_window)) == "on"

        zone = round((floor + mid) / 2.0, 1)            # the floor-to-mid sleeping zone
        E = self._equilibrium(kitchen, mid, floor)
        target = self._calc_target(E, ceiling)
        deficit = floor - target
        floor_limited = target <= self.min_temp + 0.05  # hot night: cooling as deep as the unit allows
        minutes_needed = max(0.0, deficit) / self.floor_cool_cph * 60.0

        # past bedtime -> done for the night (the AC comes out)
        if now >= bedtime:
            await self._ensure_off(
                "done_for_tonight",
                "Past bedtime -- AC off. Move the unit to the bathroom and seal the bedroom.",
                self._attrs(floor, mid, zone, ceil_s, ac_s, bath, kitchen, E, target, deficit,
                            ceiling, price_now, window_open, bedtime, 0, None, 0.0, floor_limited,
                            ceiling_base),
                notify_bedtime=True, bedtime=bedtime,
            )
            return

        cool_now, next_start, run_min, est_cost = self._schedule(now, bedtime, minutes_needed, price_at)
        slots_left = int((bedtime - now).total_seconds() // 900)
        time_constrained = run_min >= max(1, slots_left) * 15
        backleak = bath is not None and bath >= self.bathroom_max

        # decision
        if floor <= self.min_temp:
            want, reason = False, f"Floor at min ({floor:.1f}<= {self.min_temp:.1f}) -- holding"
        elif deficit <= 0.05:
            want, reason = False, f"On track: floor {floor:.1f}C <= target {target:.1f}C (zone {zone:.1f}, cap {ceiling:.0f})"
        elif not window_open:
            want, reason = False, "Bathroom window closed -- open it so the condenser can vent"
        elif backleak and not time_constrained:
            want, reason = False, f"Easing off: bathroom {bath:.1f}C would back-leak -- letting it vent"
        elif cool_now:
            want = True
            reason = (f"Pre-cool floor {floor:.1f}->{target:.1f}C (keep zone <= {ceiling:.0f} for "
                      f"{self.sleep_hours:.0f}h): ~{run_min} min in the cheapest hours, ~{est_cost:.1f} kr, "
                      f"price {price_now:.2f}" + ("  [floor-limited: hottest it can do]" if floor_limited else ""))
        else:
            nx = next_start.strftime("%H:%M") if next_start else "later"
            want, reason = False, (f"Hold for cheaper power: need ~{run_min} min, start ~{nx} "
                                   f"(floor {floor:.1f}->{target:.1f}C)")

        attrs = self._attrs(floor, mid, zone, ceil_s, ac_s, bath, kitchen, E, target, deficit,
                            ceiling, price_now, window_open, bedtime, run_min, next_start, est_cost, floor_limited,
                            ceiling_base)

        if reason != self._last_reason:
            self.log(f"PLAN {reason}", level="INFO")
            self._last_reason = reason

        if want:
            await self._apply_cool(reason, "cooling_dryrun" if self.dry_run else "cooling", attrs)
        else:
            await self._ensure_off("waiting" if deficit > 0.05 else "idle", reason, attrs)

    def _attrs(self, floor, mid, zone, ceil_s, ac_s, bath, kitchen, E, target, deficit,
               ceiling, price_now, window_open, bedtime, run_min, next_start, est_cost, floor_limited,
               ceiling_base):
        def r1(v):
            return round(v, 1) if v is not None else None
        return {
            "floor": r1(floor), "mid_wall": r1(mid), "sleeping_zone": zone,
            "ceiling_delivery": r1(ceil_s), "ac_output": r1(ac_s),
            "bathroom": r1(bath), "kitchen": r1(kitchen),
            "equilibrium_est": r1(E), "floor_target": r1(target), "deficit": round(max(0.0, deficit), 1),
            "floor_limited": floor_limited, "night_ceiling": r1(ceiling),
            "ceiling_base": r1(ceiling_base), "ceiling_source": ("comfort layer" if ceiling < ceiling_base else "knob"),
            "min_temp": r1(self.min_temp),
            "rise_frac": round(self._rise_frac, 2), "rise_samples": self._rise_samples,
            "price_now": round(price_now, 2), "window_open": window_open,
            "minutes_needed": run_min, "next_start": next_start.strftime("%H:%M") if next_start else None,
            "est_cost_kr": est_cost, "bedtime": bedtime.strftime("%H:%M"), "dry_run": self.dry_run,
        }

    # ---------- actuation (gentle; respects dry_run + anti-short-cycle) ----------
    async def _apply_cool(self, reason, status, attrs):
        await self._publish(status, reason, attrs)
        if self.dry_run:
            self.log(f"DRY-RUN would COOL ({self.cool_setpoint}C/{self.cool_fan}): {reason}")
            return
        cur_mode = await self._state(self.climate_entity)
        need_mode = cur_mode != "cool"
        if need_mode and not self._can_switch(True):
            return
        try:
            if need_mode:
                await self.call_service("climate/set_hvac_mode", entity_id=self.climate_entity, hvac_mode="cool")
                self._mark_switch("cool")
                self.log(f"COOL on ({self.cool_setpoint}C/{self.cool_fan}): {reason}", level="INFO")
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

    async def _ensure_off(self, status, reason, attrs, notify_bedtime=False, bedtime=None):
        await self._publish(status, reason, attrs)
        if notify_bedtime and not self.dry_run:
            await self._maybe_notify_bedtime(bedtime)
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
    async def _maybe_notify_bedtime(self, bedtime):
        key = bedtime.strftime("%Y-%m-%d") if bedtime else None
        if not key or self._notified_bedtime_for == key:
            return
        self._notified_bedtime_for = key
        await self._notify("Pre-cool done -- switch off, move the unit to the bathroom and seal the bedroom.")

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
            await self.set_state(self.status_entity, state=status, attributes=a, replace=True)
        except Exception as e:
            self.log(f"publish failed: {e}", level="WARNING")
