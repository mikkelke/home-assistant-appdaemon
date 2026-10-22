"""The cheapest start for a due run against the published hourly prices, for the washer's drum clean and
the dishwasher's hygiene run on sensor.house_upcoming. Pure.

Prices are the price entity's `raw_today`, plus `raw_tomorrow` once `tomorrow_valid`; a price
outside PRICE_RANGE leaves its hour unpriced, and a point off the hour is ignored. Forecast hours
never choose a start. The energy profile comes from sensor.appliance_run_profiles, in 0.1 Wh per
minute from power onset. Starts are tried at the next whole minute and then on every quarter hour,
as long as every minute of the run falls in a priced hour, so the search ends with today until
tomorrow's prices are out and with tomorrow after that. The cheapest start wins however little it
saves; a saving that rounds to 0,00 kr is none, so starting now wins.

The drum clean is priced on the first of its programmes that has a profile (drum_start). The hygiene
run is set going with the dishwasher's Timer (timer_start), which delays a start by 30 minutes to 24
hours, in half hours up to 3 hours and in whole hours after. Besides now it tries the starts a quarter
hour to 24 hours away and names the Timer setting nearest to the best one. It is priced on its own
programme's profile, else on that of the published programme that uses the most energy, which the row
says. Before PRICES_OUT, while tomorrow's prices are not out, a best start after them is not named:
they decide it.
"""

import math
from datetime import datetime, time, timezone
from typing import NamedTuple, Optional

from run_profile_model import TYPICAL  # apps/appliances - AppDaemon puts every app dir on sys.path
from upcoming_maint import HOUSE_TZ

PRICE_RANGE = (-5.0, 15.0)
START_STEP_MIN = 15
PROFILE_MAX_MIN = 720
EPSILON_KR = 1e-9
PRICES_OUT = time(13, 0)
TIMER_FINE_STEP_MIN = 30
TIMER_FINE_MAX_MIN = 3 * 60
TIMER_COARSE_STEP_MIN = 60
TIMER_MAX_MIN = 24 * 60


class Start(NamedTuple):
    """How a due row is timed: when (None if it is not), what that start saves, whether it is now and,
    for a run started with the Timer, its setting and the stand-in profile that priced it."""
    at: Optional[datetime]
    price: str
    at_now: bool
    timer: str = ""
    basis: str = ""


def tomorrow_out(attributes):
    """Whether the price entity has published tomorrow's prices."""
    return isinstance(attributes, dict) and attributes.get("tomorrow_valid") is True


def hour_prices(attributes):
    """{epoch hour: kr/kWh} from the price entity's attributes."""
    attrs = attributes if isinstance(attributes, dict) else {}
    points = list(attrs.get("raw_today") or [])
    if tomorrow_out(attrs):
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


def published_profiles(attributes, appliance):
    """{programme key: (label, [Wh per minute])} of the programmes the run profiles publish for `appliance`."""
    attrs = attributes if isinstance(attributes, dict) else {}
    if str(attrs.get("schema")) != "1":
        return {}
    entry = (attrs.get("appliances") or {}).get(appliance)
    programmes = entry.get("programmes") if isinstance(entry, dict) else None
    published = {}
    for programme in programmes or []:
        if not isinstance(programme, dict) or not isinstance(programme.get("key"), str):
            continue
        raw = programme.get("p")
        if not isinstance(raw, str):
            continue
        try:
            wh = [int(part) / 10 for part in raw.replace(" ", "").split(",")]
        except ValueError:
            continue
        if 0 < len(wh) <= PROFILE_MAX_MIN and min(wh) >= 0:
            label = programme.get("label")
            published[programme["key"]] = (label if isinstance(label, str) and label else programme["key"], wh)
    return published


def programme_profile(attributes, appliance, keys):
    """(key, [Wh per minute]) of the first of `keys` the run profiles publish for `appliance`, or None."""
    published = published_profiles(attributes, appliance)
    for key in keys:
        if key in published:
            return key, published[key][1]
    return None


def timer_profile(attributes, appliance, key):
    """(label, [Wh per minute], whether it is the programme's own) that prices a run of programme `key`:
    its profile, else that of the published programme using the most energy besides the pooled typical
    one, or None without either."""
    published = published_profiles(attributes, appliance)
    if key in published:
        return (*published[key], True)
    others = [profile for name, profile in published.items() if name != TYPICAL]
    if not others:
        return None
    label, wh = max(others, key=lambda profile: sum(profile[1]))
    return label, wh, False


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


def _first_minute(now):
    """The epoch minute of the next whole minute, which is when a start now happens."""
    return math.ceil(now.timestamp() / 60)


def cheapest_start(wh, prices, now, min_delay=0, max_delay=None):
    """(start, saving) for a run of `wh`, or None when no start is priced throughout. `saving` is the
    kr saved against starting now, rounded to the øre: 0 when starting now wins, None when starting
    now is unpriced. A start other than now is at least `min_delay` and at most `max_delay` (none:
    unlimited) minutes after it."""
    if not wh or not prices:
        return None
    cumulative = [0.0]
    for energy in wh:
        cumulative.append(cumulative[-1] + energy)
    first = _first_minute(now)
    latest = (max(prices) + 1) * 60 - len(wh)
    if max_delay is not None:
        latest = min(latest, first + max_delay)
    grid = range((first // START_STEP_MIN + 1) * START_STEP_MIN, latest + 1, START_STEP_MIN)
    starts = [first] + [start for start in grid if start - first >= min_delay]
    base = _cost(cumulative, prices, first)
    best = None
    for start in starts:
        cost = _cost(cumulative, prices, start)
        if cost is not None and (best is None or cost < best[1] - EPSILON_KR):
            best = (start, cost)
    if best is None:
        return None
    saving = None if base is None else round((base - best[1]) * 100) / 100
    if saving == 0:
        best, saving = (first, base), 0
    return datetime.fromtimestamp(best[0] * 60, timezone.utc), saving


def _saving_text(saving):
    return f"saves {saving:.2f} kr vs now".replace(".", ",")


def drum_start(price_attributes, profile_attributes, keys, now, appliance="washer"):
    """(start, text, at_now) for the drum row, or None without a priced start: when to time the row,
    what that start saves, and whether it is now."""
    profile = programme_profile(profile_attributes, appliance, keys)
    found = cheapest_start(profile[1], hour_prices(price_attributes), now) if profile else None
    if found is None:
        return None
    start, saving = found
    if saving == 0:
        return start, "cheapest now", True
    if saving is None:
        return start, f"cheapest start {start.astimezone(HOUSE_TZ):%H:%M}", False
    return start, _saving_text(saving), False


def timer_minutes(delay):
    """The Timer setting, in minutes, nearest to a start `delay` minutes away, up to 24 hours: 0, which
    is no Timer, under half the smallest step."""
    step = TIMER_FINE_STEP_MIN if delay <= TIMER_FINE_MAX_MIN else TIMER_COARSE_STEP_MIN
    return (delay + step // 2) // step * step


def timer_label(minutes):
    """A Timer setting as the machine counts it: "30 min", "1,5 h", "7 h"."""
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes / 60:g}".replace(".", ",") + " h"


def timer_start(price_attributes, profile_attributes, key, now, appliance="dishwasher"):
    """The Start for a run of programme `key` set going with the Timer, or None without a priced start.
    Its timer is the setting nearest to the best start's distance from the next whole minute."""
    profile = timer_profile(profile_attributes, appliance, key)
    if profile is None:
        return None
    label, wh, own = profile
    found = cheapest_start(wh, hour_prices(price_attributes), now, TIMER_FINE_STEP_MIN // 2, TIMER_MAX_MIN)
    if found is None:
        return None
    start, saving = found
    basis = "" if own else f"Priced on {label}, {key.capitalize()} has too few runs"
    waits = now.astimezone(HOUSE_TZ).time() < PRICES_OUT <= start.astimezone(HOUSE_TZ).time()
    if waits and not tomorrow_out(price_attributes):
        return Start(None, f"best start after {PRICES_OUT:%H:%M} prices", False, basis=basis)
    if saving == 0:
        return Start(start, "cheapest now", True, basis=basis)
    timer = timer_minutes(round(start.timestamp() / 60) - _first_minute(now))
    return Start(start, "" if saving is None else _saving_text(saving), False, f"timer {timer_label(timer)}", basis)
