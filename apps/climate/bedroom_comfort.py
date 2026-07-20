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
  ceiling_base         - fixed comfort anchor (~23 C; NOT a knob - the old
                         stepper was set as a target and poisoned the math)
  ceiling_effective    - anchor lowered when the projected morning is humid and
                         when two people share the bed (shared climate_model fn;
                         SmartCooling now computes its OWN copy of this rather
                         than reading it back here -- the old cross-app cycle)
  projected_peak / verdict / ac_worth - a thin, window-aware read of
                         sensor.sleep_plan (SmartCooling's cheapest-path planner);
                         the duplicate projection + verdict that used to live here
                         are gone (superseded by that one shared planner)
  vent_helps + vent_reason - venting only helps when outdoor air is both
                         cooler AND drier than the room
  reason / source_entities / computed_at - middle-layer convention

Data flow (one direction only, cycle broken): this app READS SmartCooling's
published rise_frac (sensor.smart_cooling_status) and sensor.sleep_plan; it no
longer opens SmartCooling's state file, and SmartCooling no longer reads this
sensor's ceiling_effective.

Consumers: dashboard Sleep-cooling card.
"""

from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

# ---------------------------------------------------------------- pure math
# The comfort/vent math now lives in the shared climate_model module (single source of
# truth, unit-testable without AppDaemon). Re-exported here so the existing test surface
# (bc.dew_point_c / project_morning_dp / effective_ceiling / hours_until_morning / classify
# / vent_helps) and any dashboard/consumer imports keep resolving unchanged. The night
# projection + deploy verdict that used to live here (project_zone_peak / ac_worth) are
# GONE -- superseded by sensor.sleep_plan, which smart_cooling publishes from the same
# shared coast/planner math; this app now just renders that plan (window-aware).
from climate_model import (  # noqa: F401  (re-exported for tests + consumers)
    dew_point_c,
    project_morning_dp,
    effective_ceiling,
    hours_until_morning,
    classify,
    vent_helps,
)


# ---------------------------------------------------------------- the app

class BedroomComfort(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.temp_entity = a("temperature_entity", "sensor.bedroom_median_temperature")
        self.rh_entity = a("humidity_entity", "sensor.bedroom_humidity")
        self.out_temp_entity = a("outdoor_temperature_entity", "sensor.gw2000a_outdoor_temperature")
        self.out_rh_entity = a("outdoor_humidity_entity", "sensor.gw2000a_humidity")
        self.persons = list(a("persons", ["person.mikkel", "person.kristine"]))
        # The tolerance anchor is comfort science, not a knob: ~23 C is the max a
        # sealed bedroom can be at night before sleep degrades (user spec
        # 2026-06-25: "23 is the ABSOLUTE max"). Humidity/occupancy lower the
        # EFFECTIVE ceiling from here. (The old stepper was set to 20.0 as a
        # *target*, which turned the effective ceiling into nonsense - knob removed.)
        self.comfort_anchor = float(a("comfort_anchor_c", 23.0))
        # Night projection is now owned by SmartCooling's cheapest-path planner
        # (sensor.sleep_plan); this app reads that plan instead of recomputing it. The
        # floor/mid/kitchen entities are kept only for source_entities provenance + the
        # existing re-eval listeners.
        self.floor_entity = a("floor_entity", "sensor.bedroom_floor_thermometer_temperature")
        self.mid_entity = a("mid_entity", "sensor.bedroom_temperature")
        self.kitchen_entity = a("kitchen_entity", "sensor.kitchen_temperature")
        # rise_frac stays LEARNED + OWNED by SmartCooling; read it from the PUBLISHED
        # attribute on sensor.smart_cooling_status (no longer from its state file). Now a
        # display-only passthrough (project_zone_peak is gone), kept to preserve the surface.
        self.rise_frac_fallback = float(a("rise_frac_fallback", 0.5))
        self.status_entity = a("status_entity", "sensor.smart_cooling_status")
        self.sleep_plan_entity = a("sleep_plan_entity", "sensor.sleep_plan")
        self.dp_rate = float(a("dp_rate_per_sleeper_c_per_h", 0.25))
        self.knee = float(a("dp_comfort_knee_c", 12.0))
        self.penalty = float(a("dp_penalty_per_c", 0.15))
        self.second_sleeper = float(a("second_sleeper_penalty_c", 0.5))
        self.max_reduction = float(a("max_ceiling_reduction_c", 1.5))
        self.publish_entity = a("publish_entity", "sensor.bedroom_comfort")

        for ent in (self.temp_entity, self.rh_entity, self.out_temp_entity,
                    self.out_rh_entity, self.floor_entity, self.kitchen_entity,
                    *self.persons):
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

    def _rise_frac(self):
        """SmartCooling's self-learned coast fraction, read from its PUBLISHED attribute on
        sensor.smart_cooling_status (not its state file -- that read was half the old
        cross-app cycle). Fallback until the attribute exists. Display-only passthrough now."""
        try:
            v = self.get_state(self.status_entity, attribute="rise_frac")
            return float(v) if v not in (None, "unknown", "unavailable") else self.rise_frac_fallback
        except (TypeError, ValueError):
            return self.rise_frac_fallback

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
            base = self.comfort_anchor

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

            rise = self._rise_frac()   # display-only passthrough of the published attribute

            # projected_peak / verdict / ac_worth are now a thin, window-aware read of
            # SmartCooling's cheapest-path planner (sensor.sleep_plan). recommendation is one
            # of windows|ac|hybrid|nothing; headline is the plain-words verdict; projected_peak
            # is the coast peak the planner already computed from the weather equilibrium.
            plan_rec = self.get_state(self.sleep_plan_entity)
            plan_headline = self.get_state(self.sleep_plan_entity, attribute="headline")
            plan_peak = self.get_state(self.sleep_plan_entity, attribute="projected_peak")
            ac_worth = plan_rec in ("ac", "hybrid")
            if plan_headline not in (None, "unknown", "unavailable"):
                verdict = plan_headline
            elif plan_rec in (None, "unknown", "unavailable"):
                verdict = "sleep plan pending"
            else:
                verdict = plan_rec
            try:
                peak = round(float(plan_peak), 1)
            except (TypeError, ValueError):
                peak = None

            if dp_morning is not None and reduction > 0:
                reason = (f"projected morning dew point {dp_morning:.1f}C with "
                          f"{sleepers} sleeper(s) -> ceiling {base:.1f} - {reduction:.2f} "
                          f"= {ceiling_eff:.1f}C")
            elif dp_in is None:
                reason = "bedroom humidity unavailable - ceiling untouched"
            else:
                reason = f"dry night projected (DP {dp_morning:.1f}C) - ceiling stays {base:.1f}C"

            # ceiling_reduction/vent_helps/ac_worth silently drop from published attributes
            # whenever they're False/0 (all three are legitimately that on ordinary calm nights;
            # confirmed live 2026-07-15: ceiling_reduction/vent_helps both absent right now) --
            # AppDaemon 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self.set_state(self.publish_entity, state=state, replace=True, attributes={
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
                "projected_peak": None if peak is None else round(peak, 1),
                "rise_frac": round(rise, 2),
                "ac_worth": ac_worth,
                "verdict": verdict,
                "vent_helps": vent_ok,
                "vent_reason": vent_reason,
                "outdoor_temperature": None if t_out is None else round(t_out, 1),
                "outdoor_dew_point": None if dp_out is None else round(dp_out, 1),
                "reason": reason,
                "source_entities": [self.temp_entity, self.rh_entity,
                                    self.out_temp_entity, self.out_rh_entity,
                                    self.floor_entity, self.mid_entity,
                                    self.kitchen_entity, *self.persons],
                "computed_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as e:
            self.log(f"comfort eval failed: {e}", level="ERROR")
