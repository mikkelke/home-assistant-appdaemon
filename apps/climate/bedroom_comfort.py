"""
Bedroom comfort middle layer - publishes ``sensor.bedroom_comfort``.

Interprets temperature + humidity + occupancy into sleep-comfort terms.
Born 2026-07-12 after a sweaty night the dry-bulb model called "survivable":
the room peaked at a modest 23.8 C, but two sleepers in a sealed room pushed
the dew point from 9.7 to 14.2 overnight. Comfort is temperature AND moisture.

Published state: comfortable / warm / sticky / hot
Key attributes:
  dew_point            - now, from bedroom T/RH (Magnus formula)
  dew_point_morning    - projected at 07:00 (sleepers exhale ~0.25 C dew point
                         per hour each in a sealed room; calibrated 2026-07-09)
  ceiling_base         - the user's night-ceiling knob
  ceiling_effective    - base lowered when the projected morning is humid and
                         when two people share the bed; SmartCooling consumes
                         THIS instead of the raw knob
  vent_helps + vent_reason - venting only helps when outdoor air is both
                         cooler AND drier than the room
  reason / source_entities / computed_at - middle-layer convention

Consumers: SmartCooling (ceiling_effective), dashboard SmartCoolingCard.
"""

import math
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

# ---------------------------------------------------------------- pure math
# Module-level so apps/climate/tests can exercise them without AppDaemon.

def dew_point_c(t_c, rh_pct):
    """Magnus formula dew point. Returns None on invalid input."""
    try:
        t = float(t_c)
        rh = float(rh_pct)
    except (TypeError, ValueError):
        return None
    if rh <= 0 or rh > 100:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + a * t / (b + t)
    return b * gamma / (a - gamma)


def project_morning_dp(dp_now, sleepers, hours, rate_per_sleeper_c_per_h):
    """Dew point after `hours` of sleepers adding moisture to a sealed room."""
    if dp_now is None:
        return None
    hours = max(0.0, min(10.0, float(hours)))
    return dp_now + rate_per_sleeper_c_per_h * max(0, int(sleepers)) * hours


def effective_ceiling(base, dp_morning, sleepers, knee_c=12.0,
                      penalty_per_c=0.15, second_sleeper_c=0.5,
                      max_reduction_c=1.5):
    """Night ceiling lowered for projected humidity and a second sleeper.
    Bounded: never more than max_reduction_c below the base."""
    reduction = 0.0
    if dp_morning is not None:
        reduction += penalty_per_c * max(0.0, dp_morning - knee_c)
    if sleepers >= 2:
        reduction += second_sleeper_c
    reduction = min(max_reduction_c, reduction)
    return round(base - reduction, 1), round(reduction, 2)


def vent_helps(t_in, dp_in, t_out, dp_out):
    """Venting helps only when outdoor air is BOTH cooler and drier.
    2026-07-09 lesson: 16 C / DP 9 outdoor air went unused while the room
    moistened - but on a muggy night venting would import water."""
    if None in (t_in, dp_in, t_out, dp_out):
        return None, "outdoor or indoor data missing"
    if t_out >= t_in - 0.5:
        return False, f"outdoor {t_out:.1f}C not cooler than bedroom {t_in:.1f}C"
    if dp_out > dp_in - 1.0:
        return False, f"outdoor dew point {dp_out:.1f}C too humid vs indoor {dp_in:.1f}C"
    return True, (f"outdoor {t_out:.1f}C / DP {dp_out:.1f}C is cooler and drier "
                  f"than bedroom {t_in:.1f}C / DP {dp_in:.1f}C")


def classify(t, dp_now, ceiling_base, ceiling_eff):
    """Human-comfort label on ABSOLUTE anchors - deliberately not the planning
    knob: with the knob at 20 a perfectly nice 20.8 C room read as "hot"
    (2026-07-12). The knob steers SmartCooling; this label describes the room."""
    del ceiling_base, ceiling_eff  # planning inputs, not comfort anchors
    if t is None:
        return "unknown"
    if t >= 24.5:
        return "hot"
    if dp_now is not None and dp_now >= 13.5:
        return "sticky"
    if t >= 23.0:
        return "warm"
    return "comfortable"


def project_zone_peak(floor_c, kitchen_c, mid_c, rise_frac, person_offset_c=0.5,
                      zone_offset_c=1.0):
    """Sealed-night sleeping-zone peak if nobody cools (SmartCooling coast law:
    the floor mass drifts toward the equilibrium E by fraction rise_frac over
    the night; the sleeping zone rides zone_offset above the floor)."""
    vals = [v for v in (floor_c, kitchen_c, mid_c) if v is not None]
    if floor_c is None or not vals:
        return None
    equilibrium = max(vals) + person_offset_c
    return floor_c + (equilibrium - floor_c) * rise_frac + zone_offset_c


def ac_worth(projected_peak, ceiling_eff, dp_morning,
             peak_margin_c=0.2, dp_oppressive_c=17.5):
    """Deploy verdict: the projected night breaks the (humidity-adjusted)
    tolerance ceiling, OR the moisture alone makes the night oppressive
    (the AC is also the dehumidifier)."""
    reasons = []
    if projected_peak is not None and ceiling_eff is not None and \
            projected_peak > ceiling_eff + peak_margin_c:
        reasons.append(f"projected {projected_peak:.1f}C exceeds the "
                       f"{ceiling_eff:.1f}C comfort ceiling")
    if dp_morning is not None and dp_morning >= dp_oppressive_c:
        reasons.append(f"morning dew point {dp_morning:.1f}C is oppressive "
                       f"(only the AC dries the room)")
    return (len(reasons) > 0), " and ".join(reasons)


def hours_until_morning(now, morning_hour=7):
    """Hours from `now` to the next 07:00, capped at 10 (projection horizon)."""
    target_day = now
    if now.hour >= morning_hour:
        from datetime import timedelta
        target_day = now + timedelta(days=1)
    target = target_day.replace(hour=morning_hour, minute=0, second=0, microsecond=0)
    hours = (target - now).total_seconds() / 3600.0
    return max(0.0, min(10.0, hours))


# ---------------------------------------------------------------- the app

class BedroomComfort(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.temp_entity = a("temperature_entity", "sensor.bedroom_median_temperature")
        self.rh_entity = a("humidity_entity", "sensor.bedroom_humidity")
        self.out_temp_entity = a("outdoor_temperature_entity", "sensor.gw2000a_outdoor_temperature")
        self.out_rh_entity = a("outdoor_humidity_entity", "sensor.gw2000a_humidity")
        self.persons = list(a("persons", ["person.mikkel", "person.kristine"]))
        self.ceiling_entity = a("night_ceiling_entity", "input_number.smart_cooling_night_ceiling")
        self.default_ceiling = float(a("default_night_ceiling", 23.0))
        self.dp_rate = float(a("dp_rate_per_sleeper_c_per_h", 0.25))
        self.knee = float(a("dp_comfort_knee_c", 12.0))
        self.penalty = float(a("dp_penalty_per_c", 0.15))
        self.second_sleeper = float(a("second_sleeper_penalty_c", 0.5))
        self.max_reduction = float(a("max_ceiling_reduction_c", 1.5))
        self.publish_entity = a("publish_entity", "sensor.bedroom_comfort")

        for ent in (self.temp_entity, self.rh_entity, self.out_temp_entity,
                    self.out_rh_entity, *self.persons):
            self.listen_state(self._on_change, ent)
        self.run_every(self._tick, "now+5", 300)

    # -- helpers
    def _num(self, entity):
        try:
            v = self.get_state(entity)
            return float(v) if v not in (None, "unknown", "unavailable") else None
        except (TypeError, ValueError):
            return None

    def _sleepers(self):
        home = sum(1 for p in self.persons if self.get_state(p) == "home")
        return max(1, home)

    # -- handlers
    def _on_change(self, entity, attribute, old, new, kwargs):
        self._eval()

    def _tick(self, kwargs):
        self._eval()

    def _eval(self):
        try:
            t_in = self._num(self.temp_entity)
            rh_in = self._num(self.rh_entity)
            t_out = self._num(self.out_temp_entity)
            rh_out = self._num(self.out_rh_entity)
            base = self._num(self.ceiling_entity)
            if base is None:
                base = self.default_ceiling

            dp_in = dew_point_c(t_in, rh_in)
            dp_out = dew_point_c(t_out, rh_out)
            sleepers = self._sleepers()
            hours = hours_until_morning(self.datetime())
            dp_morning = project_morning_dp(dp_in, sleepers, hours, self.dp_rate)
            ceiling_eff, reduction = effective_ceiling(
                base, dp_morning, sleepers, self.knee, self.penalty,
                self.second_sleeper, self.max_reduction)
            vent_ok, vent_reason = vent_helps(t_in, dp_in, t_out, dp_out)
            state = classify(t_in, dp_in, base, ceiling_eff)

            if dp_morning is not None and reduction > 0:
                reason = (f"projected morning dew point {dp_morning:.1f}C with "
                          f"{sleepers} sleeper(s) -> ceiling {base:.1f} - {reduction:.2f} "
                          f"= {ceiling_eff:.1f}C")
            elif dp_in is None:
                reason = "bedroom humidity unavailable - ceiling untouched"
            else:
                reason = f"dry night projected (DP {dp_morning:.1f}C) - ceiling stays {base:.1f}C"

            self.set_state(self.publish_entity, state=state, attributes={
                "friendly_name": "Bedroom comfort",
                "icon": "mdi:bed-clock",
                "temperature": None if t_in is None else round(t_in, 1),
                "rel_humidity": None if rh_in is None else round(rh_in, 0),
                "dew_point": None if dp_in is None else round(dp_in, 1),
                "dew_point_morning": None if dp_morning is None else round(dp_morning, 1),
                "sleepers": sleepers,
                "ceiling_base": round(base, 1),
                "ceiling_effective": ceiling_eff,
                "ceiling_reduction": reduction,
                "vent_helps": vent_ok,
                "vent_reason": vent_reason,
                "outdoor_temperature": None if t_out is None else round(t_out, 1),
                "outdoor_dew_point": None if dp_out is None else round(dp_out, 1),
                "reason": reason,
                "source_entities": [self.temp_entity, self.rh_entity,
                                    self.out_temp_entity, self.out_rh_entity,
                                    *self.persons],
                "computed_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as e:
            self.log(f"comfort eval failed: {e}", level="ERROR")
