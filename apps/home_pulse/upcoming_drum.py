"""The cheapest start for the washer drum clean against the published hourly prices, for the drum
row of sensor.house_upcoming. Pure.

Prices are the price entity's `raw_today`, plus `raw_tomorrow` once `tomorrow_valid`; a price
outside PRICE_RANGE leaves its hour unpriced, and a point off the hour is ignored. The energy
profile is the first of the given programmes that sensor.appliance_run_profiles publishes, in
0.1 Wh per minute from power onset. Starts are tried at the next whole minute and then on every
quarter hour, as long as every minute of the run falls in a priced hour, so the search ends with
today until tomorrow's prices are out and with tomorrow after that. A later start is offered only
when it saves at least SAVING_MIN_KR and SAVING_MIN_PCT of starting now, the dashboard planner's
saving rule; otherwise starting now is.
"""

import math
from datetime import datetime, timezone

from upcoming_maint import HOUSE_TZ

PRICE_RANGE = (-5.0, 15.0)
START_STEP_MIN = 15
PROFILE_MAX_MIN = 720
SAVING_MIN_KR = 0.10
SAVING_MIN_PCT = 10
EPSILON_KR = 1e-9


def hour_prices(attributes):
    """{epoch hour: kr/kWh} from the price entity's attributes."""
    attrs = attributes if isinstance(attributes, dict) else {}
    points = list(attrs.get("raw_today") or [])
    if attrs.get("tomorrow_valid") is True:
        points += list(attrs.get("raw_tomorrow") or [])
    prices = {}
    for point in points:
        if not isinstance(point, dict):
            continue
        hour, price = point.get("hour"), point.get("price")
        if isinstance(hour, str):
            try:
                hour = datetime.fromisoformat(hour)
            except ValueError:
                continue
        if not isinstance(hour, datetime) or hour.tzinfo is None or isinstance(price, bool):
            continue
        if not isinstance(price, (int, float)) or not PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
            continue
        seconds = hour.timestamp()
        if seconds % 3600 == 0:
            prices.setdefault(int(seconds // 3600), float(price))
    return prices


def programme_profile(attributes, appliance, keys):
    """(key, [Wh per minute]) of the first of `keys` the run profiles publish for `appliance`, or None."""
    attrs = attributes if isinstance(attributes, dict) else {}
    if str(attrs.get("schema")) != "1":
        return None
    entry = (attrs.get("appliances") or {}).get(appliance)
    programmes = entry.get("programmes") if isinstance(entry, dict) else None
    published = {p.get("key"): p for p in programmes or [] if isinstance(p, dict)}
    for key in keys:
        raw = (published.get(key) or {}).get("p")
        if not isinstance(raw, str):
            continue
        try:
            wh = [int(part) / 10 for part in raw.replace(" ", "").split(",")]
        except ValueError:
            continue
        if 0 < len(wh) <= PROFILE_MAX_MIN and min(wh) >= 0:
            return key, wh
    return None


def _cost(cumulative, prices, start):
    """kr for a run starting at epoch minute `start`, or None when a minute of it is unpriced."""
    length, kr, minute = len(cumulative) - 1, 0.0, 0
    while minute < length:
        hour = (start + minute) // 60
        price = prices.get(hour)
        if price is None:
            return None
        end = min(length, (hour + 1) * 60 - start)
        kr += (cumulative[end] - cumulative[minute]) / 1000 * price
        minute = end
    return kr


def _passes(base, cost):
    saving = base - cost
    return saving + EPSILON_KR >= SAVING_MIN_KR and (base <= 0 or saving + EPSILON_KR >= base * SAVING_MIN_PCT / 100)


def cheapest_start(wh, prices, now):
    """(start, saving) for a run of `wh`, or None when no start is priced throughout. `saving` is the
    kr saved against starting now: 0 when starting now wins, None when starting now is unpriced."""
    if not wh or not prices:
        return None
    cumulative = [0.0]
    for energy in wh:
        cumulative.append(cumulative[-1] + energy)
    first = math.ceil(now.timestamp() / 60)
    latest = (max(prices) + 1) * 60 - len(wh)
    starts = [first] + list(range((first // START_STEP_MIN + 1) * START_STEP_MIN, latest + 1, START_STEP_MIN))
    base = _cost(cumulative, prices, first)
    best = None
    for start in starts:
        cost = _cost(cumulative, prices, start)
        if cost is not None and (best is None or cost < best[1] - EPSILON_KR):
            best = (start, cost)
    if best is None:
        return None
    if base is not None and best[0] != first and not _passes(base, best[1]):
        best = (first, base)
    saving = None if base is None else base - best[1]
    return datetime.fromtimestamp(best[0] * 60, timezone.utc), saving


def drum_start(price_attributes, profile_attributes, keys, now, appliance="washer"):
    """(start, text) the drum row is timed at and says, or None without a priced start."""
    profile = programme_profile(profile_attributes, appliance, keys)
    found = cheapest_start(profile[1], hour_prices(price_attributes), now) if profile else None
    if found is None:
        return None
    start, saving = found
    if saving == 0:
        return start, "start now"
    text = f"cheapest start {start.astimezone(HOUSE_TZ):%H:%M}"
    if saving is not None:
        text += f" - saves {saving:.2f} kr vs now".replace(".", ",")
    return start, text
