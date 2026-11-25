"""PriceForecast - sensor.electricity_price_forecast: Stromligning's forecast for the hours the price entity
(sensor.energi_data_service) has no published price for, priced the way that entity prices its own hours.

Stromligning forecasts the spot price per hour, ex VAT, in kr/kWh. Each hour becomes
    (spot + supplier_addon) x (1 + vat) + tariffs_incl_vat[local hour]
as the price entity prices raw_today: supplier_addon is its cost template (ex VAT) and the tariffs are its `tariffs`
attribute, the hour's tariff plus every additional one. The price entity's unit is kept, kr/kWh or ore/kWh (use_cent).

Entity contract:
  state       "ok" while the forecast comes from the latest fetch, "stale" while that fetch has failed and the last
              good forecast stays, "unknown" until a fetch has worked
  attributes  source "stromligning"; updated, the local time of the last good fetch; reason, a sentence on the state;
              forecast, [{hour, price}] in the shape of the price entity's own forecast attribute: hour a local
              ISO 8601 hour start with its offset, price the all-in price to 3 decimals. One entry per hour, in
              order, from the first day without a published price (tomorrow, the day after once tomorrow_valid) to
              the end of Stromligning's forecast; an hour its slots do not cover whole is left out.

Fetched at start, at each of refresh_times and when tomorrow_valid turns true (once a day), and published once the
price entity's tariffs can be read. A fetch that fails, or has no forecast hours, keeps the last good forecast; with
none, it is tried again after retry_minutes. A Home Assistant restart drops an entity made by set_state, so
plugin_started publishes it again.
"""

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

TZ = ZoneInfo("Europe/Copenhagen")
ENTITY = "sensor.electricity_price_forecast"
PRICE_ENTITY = "sensor.energi_data_service"
URL = "https://stromligning.dk/api/prices"
USER_AGENT = "home-assistant-appdaemon"
TIMEOUT_S = 20
MAX_BYTES = 8_000_000
HOUR_S = 3600
DECIMALS = 3
RESOLUTION_MINUTES = {"15m": 15, "30m": 30, "60m": 60, "1h": 60}
REFRESH_TIMES = ("06:45:00", "12:40:00", "15:30:00", "20:45:00")


def _dig(obj, *keys):
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _utc(value):
    """Aware UTC datetime from an ISO 8601 string with an offset or Z, else None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


def hourly(slots):
    """{hour start epoch: mean} from (start, minutes, value) slots. An hour is kept only when its
    slots cover exactly its 60 minutes, so a partial or doubled hour is dropped, never guessed."""
    acc = {}
    for start, minutes, value in slots:
        if start is None or value is None or not isinstance(minutes, int) or minutes <= 0:
            continue
        epoch = int(start.timestamp())
        hour = epoch - epoch % HOUR_S
        covered, total = acc.get(hour, (0, 0.0))
        acc[hour] = (covered + minutes, total + value * minutes)
    return {hour: total / covered for hour, (covered, total) in acc.items() if covered == 60}


def parse_spot(resp):
    """{hour start epoch: spot price ex VAT in kr/kWh} from a /api/prices response: the
    details.electricity.value of its slots, quarters and hours alike, averaged to the hour."""
    prices = resp.get("prices") if isinstance(resp, dict) else None
    slots = []
    for item in prices if isinstance(prices, list) else []:
        electricity = _dig(item, "details", "electricity")
        if not isinstance(electricity, dict) or electricity.get("unit") not in (None, "kr/kWh"):
            continue
        slots.append((_utc(item.get("date")), RESOLUTION_MINUTES.get(item.get("resolution")),
                      _number(electricity.get("value"))))
    return hourly(slots)


def tariff_table(tariffs):
    """The price entity's tariffs as 24 values incl. VAT by local hour (the hour's tariff plus every
    additional tariff), or None when incomplete."""
    if not isinstance(tariffs, dict):
        return None
    by_hour, extra = tariffs.get("tariffs"), tariffs.get("additional_tariffs") or {}
    if not isinstance(by_hour, dict) or not isinstance(extra, dict):
        return None
    added = [_number(value) for value in extra.values()]
    table = [_number(by_hour.get(str(hour), by_hour.get(hour))) for hour in range(24)]
    if None in added or None in table:
        return None
    return [round(value + sum(added), 6) for value in table]


def price_hours(spot, table, vat, addon, scale, first_day):
    """The forecast attribute: [{hour, price}] in order for every hour of `spot` on or after the local date
    `first_day`. `table` is tariff_table's, `addon` ex VAT, `scale` 100 for a price entity in ore."""
    points = []
    for epoch in sorted(spot):
        local = datetime.fromtimestamp(epoch, TZ)
        if local.date() >= first_day:
            price = ((spot[epoch] + addon) * (1 + vat) + table[local.hour]) * scale
            points.append({"hour": local.isoformat(), "price": round(price, DECIMALS)})
    return points


def load_spot(url, params):
    """Fetch and parse the forecast on the executor. Never raises: the answer is {"ok": True, "spot": {...}}
    or {"ok": False, "error": <short reason>}."""
    try:
        request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            spot = parse_spot(json.loads(response.read(MAX_BYTES)))
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:100]}
    if not spot:
        return {"ok": False, "error": "no whole hours in the response"}
    return {"ok": True, "spot": spot}


class PriceForecast(hass.Hass):
    def initialize(self):
        a = self.args
        self.entity = a.get("entity", ENTITY)
        self.price_entity = a.get("price_entity", PRICE_ENTITY)
        self.vat = float(a.get("vat", 0.25))
        self.addon = float(a.get("supplier_addon", 0.012))
        self.retry_s = int(float(a.get("retry_minutes", 15)) * 60)
        self.query = {
            "supplierId": str(a.get("stromligning_grid", "radius_c")),
            "priceArea": str(a.get("price_area", "DK2")),
            "productId": str(a.get("stromligning_product", "vindstoed_vestroem")),
            "forecast": "true",
        }
        self.points, self.updated, self.failure = [], None, None
        self._rise_day = None
        times = [str(clock) for clock in (a.get("refresh_times") or REFRESH_TIMES)]
        for clock in times:
            self.run_daily(self._on_tick, time.fromisoformat(clock))
        self.listen_state(self._on_tomorrow_valid, self.price_entity, attribute="tomorrow_valid")
        self.listen_event(self._on_plugin_started, "plugin_started")
        self._on_tick({})
        self.log(f"PriceForecast: Stromligning at start, {', '.join(clock[:5] for clock in times)} and on "
                 f"tomorrow_valid -> {self.entity}")

    def _on_tick(self, kwargs):
        tomorrow = self.get_now().astimezone(TZ).date() + timedelta(days=1)
        start = datetime.combine(tomorrow, time(0), TZ).astimezone(timezone.utc)
        params = {**self.query, "from": start.strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.submit_to_executor(load_spot, URL, params, callback=self._on_result)

    def _on_tomorrow_valid(self, entity, attribute, old, new, kwargs):
        today = self.get_now().astimezone(TZ).date()
        if new is True and old is not True and self._rise_day != today:
            self._rise_day = today
            self._on_tick({})

    def _on_plugin_started(self, event_name, data, kwargs):
        self._publish()

    def _on_result(self, result=None, **kwargs):
        result = result if isinstance(result, dict) else {}
        now = self.get_now().astimezone(TZ)
        if result.get("ok"):
            failure = self._accept(result.get("spot"), now)
        else:
            failure = result.get("error") or "no result"
        if failure is None:
            if self.failure is not None:
                self.log("Price forecast is fetched again", level="INFO")
            self.failure = None
            self.log(f"PriceForecast: {self._span()} from Stromligning")
        else:
            if self.failure is None:
                self.log(f"Price forecast not fetched: {failure}", level="WARNING")
            self.failure = failure
            if not self.points:
                self.run_in(self._on_tick, self.retry_s)
        self._publish()

    def _accept(self, spot, now):
        """Prices the fetched hours and keeps them. None when it worked, else why it is no use."""
        attrs = self.get_state(self.price_entity, attribute="attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        table = tariff_table(attrs.get("tariffs"))
        if table is None:
            return f"no tariffs on {self.price_entity}"
        first_day = now.date() + timedelta(days=2 if attrs.get("tomorrow_valid") is True else 1)
        scale = 100 if attrs.get("use_cent") is True else 1
        points = price_hours(spot, table, self.vat, self.addon, scale, first_day)
        if not points:
            return f"no forecast hours from {first_day:%d.%m}"
        self.points, self.updated = points, now.isoformat(timespec="seconds")
        return None

    def _span(self):
        last = datetime.fromisoformat(self.points[-1]["hour"])
        return f"{len(self.points)} h to {last:%d.%m %H}:00"

    def _reason(self):
        if not self.points:
            return f"No forecast yet: {self.failure}" if self.failure else "No forecast yet"
        fetched = f"{datetime.fromisoformat(self.updated):%H:%M}"
        if self.failure is None:
            return f"From Strømligning at {fetched}, {self._span()}"
        return f"Not fetched ({self.failure}); keeping the forecast from {fetched}, {self._span()}"

    def _publish(self):
        state = "unknown" if not self.points else "ok" if self.failure is None else "stale"
        attributes = {"friendly_name": "Electricity price forecast", "icon": "mdi:chart-line",
                      "source": "stromligning", "reason": self._reason(), "forecast": self.points}
        if self.updated is not None:
            attributes["updated"] = self.updated
        self.set_state(self.entity, state=state, attributes=attributes, replace=True, check_existence=False)
