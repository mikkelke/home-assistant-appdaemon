"""StartAdvice - the Energy Plan's default start advice for washer and dishwasher.

Reads sensor.appliance_run_profiles, sensor.energi_data_service and each appliance's
state sensor. The price entity is the grid flow's entity_energy_price in HA Energy
preferences, also used by the dashboard; price_entity may override it. Prices come
from raw_today, raw_tomorrow when tomorrow_valid is true, and forecast. Forecast
hours join the timeline but never choose a start, matching assembleAppliancePlan.

Entity contract, for key washer|dishwasher:
  sensor.<key>_best_start: device_class timestamp; recommended power-onset UTC ISO
    timestamp, or unknown without a plan (including Running/Paused).
    Plan attributes: kind now|knob|wait, knob_min, press_from (UTC ISO), press_by
    (UTC ISO, only when the window closes early enough), cost_kr, saving_kr and
    baseline_kr (absent when unknown), programme (published key), estimated.
  sensor.<key>_start_advice: spoken English, <= 200 characters, home clocks in
    Europe/Copenhagen. Running/Paused says "Already running." Missing input never
    claims that waiting saves nothing. No programme selection, deadline or dryer
    chain: the first published programme and done-by any are the dashboard defaults.

Numeric zero attributes travel as "0" and estimated as "true"/"false" to survive
AppDaemon's HTTP cleaning. Other numeric attributes remain numbers. Unknown optional
attributes are absent. Replacement writes remove obsolete plan and press-by fields.

Recomputes on initialization, every minute and on profile, price or appliance state
changes. Each entity is published only when its state or attributes change; missing
entities are recreated. No files, appliance controls or notifications are written.
The pure planner and speech rules live in start_advice_model.py.
"""

from __future__ import annotations

import time

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import start_advice_model as model


def _attributes(values):
    """Preserve zero and false through AppDaemon's attribute transport."""
    return {key: ("true" if value else "false") if isinstance(value, bool)
            else "0" if isinstance(value, (int, float)) and value == 0 else value
            for key, value in values.items() if value is not None}


class StartAdvice(hass.Hass):
    def initialize(self):
        self.profiles_entity = self.args.get("profiles_entity", model.RUN_PROFILES_ENTITY_ID)
        self.price_entity = self.args.get("price_entity", model.PRICE_ENTITY_ID)
        self.appliances = self.args.get("appliances") or {
            "washer": "sensor.washer_state", "dishwasher": "sensor.dishwasher_state"}
        self._published = {}
        for entity in (self.profiles_entity, self.price_entity, *self.appliances.values()):
            self.listen_state(self._changed, entity, attribute="all")
        self.run_every(self._recompute, "now+60", 60)
        self._recompute()

    def _changed(self, entity, attribute, old, new, kwargs):
        self._recompute()

    def _recompute(self, kwargs=None):
        try:
            profiles = model.parse_run_profiles(self.get_state(self.profiles_entity, attribute="attributes"))
            attrs = self.get_state(self.price_entity, attribute="attributes") or {}
            timeline = model.build_hourly_prices(
                model.parse_raw_points(attrs.get("raw_today")),
                model.parse_raw_points(attrs.get("raw_tomorrow")),
                attrs.get("tomorrow_valid") is True,
                model.parse_raw_points(attrs.get("forecast")))
            now_ms = int(time.time() * 1000)
            for key, entity in self.appliances.items():
                result = model.build_advice(key, profiles, timeline, now_ms, self.get_state(entity))
                self._publish(f"sensor.{key}_best_start", result["state"], result["attributes"])
                self._publish(f"sensor.{key}_start_advice", result["advice"], {})
        except Exception as exc:
            self.log(f"StartAdvice: recompute failed: {exc}", level="ERROR")

    def _publish(self, entity, state, attributes):
        payload = {"state": state, "attributes": _attributes(attributes)}
        previous = self._published.get(entity)
        if previous is None:
            current = self.get_state(entity, attribute="all")
            if current and current.get("state") == state and current.get("attributes") == payload["attributes"]:
                self._published[entity] = payload
                return
        elif previous == payload and self.get_state(entity) is not None:
            return
        try:
            self.set_state(entity, state=state, attributes=dict(payload["attributes"]),
                           replace=True, check_existence=False)
        except Exception as exc:
            self.log(f"StartAdvice: publish {entity} failed: {exc}", level="WARNING")
            return
        self._published[entity] = payload
