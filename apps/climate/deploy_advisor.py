"""
AC deploy advisor - publishes ``sensor.bedroom_night_projection`` and sends ONE
notification when a too-warm night is close enough to be worth the setup hassle.

The user tears the portable AC down between hot spells (tidy rooms) and asked
the original question this answers: "how do I know if I should prepare the A/C?"
(2026-07-10). Projection model = the A1 regression fit (2026-07-09, background
agent; chained 7-day validation peak MAE 0.46 C, target was 0.5):

  kitchen 23:00 chain (apartment mass, behavioral venting floor C=22.25):
    K' = K + 0.137*max(0, t_max - K) + 0.508*(max(22.25, t_ev) - K) + 0.799
  bedtime floor chain (AC-free nights):
    F' = F + 0.634*(K' - F) + 0.138*min(0, t_ev - F) + 0.472
  bedroom 23:00 aux:      B' = 1.062*F' + 0.063*K' - 2.918
  sealed-night coast:     floor_peak = F + (E - F) * r,  E = mean(K, B, F)
                          zone_peak  = floor_peak + zone_uplift (1.5 C)
  r (rise_frac) is self-learned by SmartCooling (smart_cooling_state.json).

Forecast: met.no hourly via weather.get_forecasts (raw WS envelope - dig
result.response.<entity>.forecast; can hang -> wait_for timeout). t_max = daily
high, t_ev = the evening 22:00 temperature.

Notification policy: when the AC is not running and a projected night breaks
the ceiling within `notify_lead_days`, notify ONCE per date (persisted).
All-clear once the horizon is comfortably cool again after a warning.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass

# ---------------------------------------------------------------- pure model
# A1 fit constants (2026-07-09). Overridable from yaml `fit:` block.
DEFAULT_FIT = {
    "k_tmax": 0.137,
    "k_ev": 0.508,
    "k_const": 0.799,
    "comfort_floor": 22.25,
    "f_k": 0.634,
    "f_ev": 0.138,
    "f_const": 0.472,
    "b_f": 1.062,
    "b_k": 0.063,
    "b_const": -2.918,
    "zone_uplift": 1.5,
}


def kitchen_chain(k, t_max, t_ev, c):
    return (k + c["k_tmax"] * max(0.0, t_max - k)
            + c["k_ev"] * (max(c["comfort_floor"], t_ev) - k) + c["k_const"])


def floor_chain(f, k_next, t_ev, c):
    return f + c["f_k"] * (k_next - f) + c["f_ev"] * min(0.0, t_ev - f) + c["f_const"]


def b23_aux(f_next, k_next, c):
    return c["b_f"] * f_next + c["b_k"] * k_next + c["b_const"]


def night_peak(f, k, b, rise_frac, c):
    e = (k + b + f) / 3.0
    return f + (e - f) * rise_frac + c["zone_uplift"]


def project_nights(k0, f0, b0, days, rise_frac, c):
    """days = [{'date', 't_max', 't_ev'}, ...]; day 0 = tonight (anchors are
    today's measured state, so tonight uses them directly). Returns one dict
    per night with the projected sleeping-zone peak at ~07:00."""
    out = []
    k, f, b = k0, f0, b0
    for i, d in enumerate(days):
        if i > 0:  # chain today's state forward through day i's weather
            k_next = kitchen_chain(k, d["t_max"], d["t_ev"], c)
            f = floor_chain(f, k_next, d["t_ev"], c)
            k = k_next
            b = b23_aux(f, k, c)
        peak = night_peak(f, k, b, rise_frac, c)
        out.append({"date": d["date"], "t_max": round(d["t_max"], 1),
                    "t_ev": round(d["t_ev"], 1), "kitchen": round(k, 1),
                    "floor": round(f, 1), "peak": round(peak, 1)})
    return out


def daily_from_hourly(hourly, today):
    """hourly = [(local_dt, temp)]; group into calendar days with t_max
    (06-23h high) and t_ev (22:00, fallback 21/23h). Skips incomplete days."""
    days = {}
    for dt, temp in hourly:
        days.setdefault(dt.date(), []).append((dt.hour, temp))
    out = []
    for date in sorted(days):
        if date < today:
            continue
        hours = dict(days[date])
        t_ev = hours.get(22, hours.get(21, hours.get(23)))
        daytime = [t for h, t in hours.items() if 6 <= h <= 23]
        if t_ev is None or not daytime:
            continue
        out.append({"date": date.isoformat(), "t_max": max(daytime), "t_ev": t_ev})
    return out


# ---------------------------------------------------------------- the app

class DeployAdvisor(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.floor_sensor = a("floor_sensor", "sensor.bedroom_floor_thermometer_temperature")
        self.mid_sensor = a("mid_sensor", "sensor.bedroom_temperature")
        self.kitchen_sensor = a("kitchen_sensor", "sensor.kitchen_temperature")
        self.weather_entity = a("weather_entity", "weather.forecast_home")
        self.ceiling_entity = a("night_ceiling_entity", "input_number.smart_cooling_night_ceiling")
        self.default_ceiling = float(a("default_night_ceiling", 23.0))
        self.ac_climate = a("ac_climate_entity", "climate.air_conditioner_thermostat")
        self.ac_power = a("ac_power_entity", "sensor.air_conditioner_real_time_power")
        self.sc_state_file = a("smart_cooling_state_file", "/conf/apps/climate/smart_cooling_state.json")
        self.state_file = a("state_file", "/conf/apps/climate/deploy_advisor_state.json")
        self.publish_entity = a("publish_entity", "sensor.bedroom_night_projection")
        self.notify_lead_days = int(a("notify_lead_days", 2))
        self.notify_target = a("notify_target", "mikkel")
        self.default_rise_frac = float(a("default_rise_frac", 0.502))
        self.fit = dict(DEFAULT_FIT)
        self.fit.update(a("fit", {}) or {})

        self._advisor_state = self._load_state()

        self.run_in(lambda kw: self.create_task(self._eval()), 20)
        for hhmm in ("12:15:00", "20:30:00"):
            self.run_daily(lambda kw: self.create_task(self._eval()), hhmm)
        self.listen_state(lambda *args, **kw: self.create_task(self._eval()),
                          self.ceiling_entity)

    # -- state persistence (which dates we already notified about)
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {"notified_date": None, "warning_armed": False}

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self._advisor_state, f)
        except Exception as e:
            self.log(f"state save failed ({e}) - continuing in-memory", level="WARNING")

    # -- helpers
    async def _num(self, entity, default=None):
        try:
            v = await self.get_state(entity)
            return float(v) if v not in (None, "unknown", "unavailable") else default
        except (TypeError, ValueError):
            return default

    def _rise_frac(self):
        try:
            with open(self.sc_state_file) as f:
                return float(json.load(f).get("rise_frac", self.default_rise_frac))
        except Exception:
            return self.default_rise_frac

    async def _hourly_forecast(self):
        """met.no hourly as [(local_dt, temp)] - envelope + hang gotchas handled."""
        resp = await asyncio.wait_for(
            self.call_service("weather/get_forecasts", entity_id=self.weather_entity,
                              type="hourly", return_response=True),
            timeout=12)
        node = resp
        for key in ("result", "response", self.weather_entity, "forecast"):
            if isinstance(node, dict) and key in node:
                node = node[key]
        if not isinstance(node, list):  # fallback: recurse for any forecast list
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
        now = await self.get_now()
        out = []
        for item in node:
            try:
                dt = datetime.fromisoformat(str(item["datetime"]).replace("Z", "+00:00"))
                out.append((dt.astimezone(now.tzinfo), float(item["temperature"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def _ac_running(self):
        power = await self._num(self.ac_power, 0.0) or 0.0
        mode = await self.get_state(self.ac_climate)
        return power > 5.0 or mode in ("cool", "dry", "fan_only", "auto", "heat")

    # -- main
    async def _eval(self):
        try:
            k0 = await self._num(self.kitchen_sensor)
            f0 = await self._num(self.floor_sensor)
            b0 = await self._num(self.mid_sensor, f0)
            if k0 is None or f0 is None:
                await self._publish("unknown", "kitchen/floor sensor unavailable", [])
                return
            ceiling = await self._num(self.ceiling_entity, self.default_ceiling)
            rise = self._rise_frac()

            hourly = await self._hourly_forecast()
            now = await self.get_now()
            days = daily_from_hourly(hourly, now.date())[:4]
            if not days:
                await self._publish("unknown", "no usable forecast", [])
                return

            nights = project_nights(k0, f0, b0, days, rise, self.fit)
            for n in nights:
                n["over_ceiling"] = n["peak"] > ceiling
            first_over = next((n for n in nights if n["over_ceiling"]), None)

            state = f"{nights[0]['peak']:.1f}"
            if first_over:
                reason = (f"first too-warm night {first_over['date']} "
                          f"(peak {first_over['peak']}C > ceiling {ceiling:.1f}C)")
            else:
                reason = f"all {len(nights)} projected nights under the {ceiling:.1f}C ceiling"
            await self._publish(state, reason, nights, ceiling, rise)

            await self._maybe_notify(first_over, nights, ceiling, now)
        except Exception as e:
            self.log(f"advisor eval failed: {e}", level="ERROR")

    async def _publish(self, state, reason, nights, ceiling=None, rise=None):
        await self.set_state(self.publish_entity, state=state, attributes={
            "friendly_name": "Bedroom night projection",
            "icon": "mdi:weather-night",
            "unit_of_measurement": "°C",
            "nights": nights,
            "ceiling": ceiling,
            "rise_frac": rise,
            "reason": reason,
            "model": "A1 fit 2026-07-09 (peak MAE 0.46C) - passive night, no AC",
            "source_entities": [self.kitchen_sensor, self.floor_sensor,
                                self.mid_sensor, self.weather_entity],
            "computed_at": datetime.now().isoformat(timespec="seconds"),
        })

    async def _maybe_notify(self, first_over, nights, ceiling, now):
        st = self._advisor_state
        if first_over:
            lead = (datetime.fromisoformat(first_over["date"]).date() - now.date()).days
            if lead <= self.notify_lead_days and await self._ac_running():
                return  # already cooling - nothing to advise
            if lead <= self.notify_lead_days and st.get("notified_date") != first_over["date"]:
                st["notified_date"] = first_over["date"]
                st["warning_armed"] = True
                self._save_state()
                day = ("tonight" if lead == 0 else
                       "tomorrow night" if lead == 1 else
                       f"{first_over['date']} ({lead} days)")
                await self._notify(
                    f"Bedroom projected {first_over['peak']}C {day} without cooling "
                    f"(ceiling {ceiling:.1f}C). Worth setting up the AC before then.")
        elif st.get("warning_armed") and all(n["peak"] <= ceiling - 0.5 for n in nights):
            st["warning_armed"] = False
            st["notified_date"] = None
            self._save_state()
            await self._notify("Nights ahead look fine again - the AC can stay packed.")

    async def _notify(self, message):
        try:
            notifier = self.get_app("MobileNotifier")
            await notifier.notify(title="AC advisor", message=message,
                                  target=self.notify_target)
            self.log(f"notified: {message}")
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")
