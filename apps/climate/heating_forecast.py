"""
Open-Meteo forecast support for the floor-heating controller: request URL, response parser,
NOAA solar position and the per-day aggregates the heat budget is built from.

Pure functions only (no AppDaemon import, no I/O), so the controller's weather logic can be
unit-tested on its own.

Hour convention: Open-Meteo stamps each hourly value with the END of its hour (shortwave
radiation is the mean of the preceding hour). Every variable is therefore treated as the hour
interval [t - 1 h, t), and an hour belongs to the local day that contains its midpoint.
Timestamps are epoch seconds; local days come from the tzinfo the caller passes in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

API_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARIABLES = ("temperature_2m", "wind_speed_10m", "shortwave_radiation", "cloud_cover")
HDD_BASE_C = 17.0
HOUR_S = 3600
MIN_DAY_HOURS = 20
FORECAST_DAYS = 4


def request_url(latitude: float, longitude: float, days: int = FORECAST_DAYS) -> str:
    """Forecast request for a location, rounded to 2 decimals. Times come back as epoch
    seconds, wind in km/h; the series starts at local midnight of today. Four days from
    midnight cover the next 72 hours at any hour of the day."""
    return (
        f"{API_URL}?latitude={latitude:.2f}&longitude={longitude:.2f}"
        f"&hourly={','.join(HOURLY_VARIABLES)}"
        f"&wind_speed_unit=kmh&timeformat=unixtime&timezone=auto&forecast_days={days}"
    )


@dataclass(frozen=True)
class Forecast:
    times: tuple  # epoch seconds at the END of each hour
    temperature: tuple  # deg C
    wind: tuple  # km/h
    ghi: tuple  # W/m2, global horizontal irradiance (mean of the preceding hour)
    cloud: tuple  # percent
    fetched_at: float  # epoch seconds

    def hour_start(self, i: int) -> float:
        return self.times[i] - HOUR_S


def _series(hourly: dict, name: str, n: int, required: bool) -> tuple:
    raw = hourly.get(name)
    if raw is None:
        if required:
            raise ValueError(f"forecast has no {name}")
        return (None,) * n
    if not isinstance(raw, list) or len(raw) != n:
        raise ValueError(f"forecast {name} does not match the time axis")
    out = []
    for v in raw:
        try:
            out.append(None if v is None else float(v))
        except (TypeError, ValueError):
            out.append(None)
    return tuple(out)


def parse_response(payload: object, fetched_at: float) -> Forecast:
    """Open-Meteo JSON (timeformat=unixtime) to a Forecast. Raises ValueError when the
    response is not usable; null values inside a series stay None."""
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise ValueError("forecast has no hourly time axis")
    times = []
    for t in hourly["time"]:
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise ValueError("forecast time axis is not epoch seconds")
        times.append(int(t))
    if not times or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("forecast time axis is empty or not increasing")
    n = len(times)
    return Forecast(
        times=tuple(times),
        temperature=_series(hourly, "temperature_2m", n, True),
        wind=_series(hourly, "wind_speed_10m", n, True),
        ghi=_series(hourly, "shortwave_radiation", n, True),
        cloud=_series(hourly, "cloud_cover", n, False),
        fetched_at=float(fetched_at),
    )


def age_minutes(fc: Forecast, now_ts: float) -> float:
    return (now_ts - fc.fetched_at) / 60.0


def is_usable(fc: Optional[Forecast], now_ts: float, stale_hours: float) -> bool:
    """A forecast is usable while it is younger than stale_hours and still covers `now`."""
    if fc is None:
        return False
    return age_minutes(fc, now_ts) <= stale_hours * 60.0 and fc.times[-1] >= now_ts


def value_at(fc: Forecast, series: tuple, ts: float) -> Optional[float]:
    """The series value of the hour whose interval [t - 1 h, t) contains ts."""
    for i, end in enumerate(fc.times):
        if end - HOUR_S <= ts < end:
            return series[i]
    return None


def solar_position(ts: float, latitude: float, longitude: float) -> tuple:
    """(azimuth, elevation) in degrees, azimuth clockwise from north, NOAA solar calculator
    without atmospheric refraction. Longitude is positive east."""
    jd = ts / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    center = (
        math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(2 * mr) * (0.019993 - 0.000101 * jc)
        + math.sin(3 * mr) * 0.000289
    )
    true_long = l0 + center
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = true_long - 0.00569 - 0.00478 * math.sin(omega)
    obliq = (
        23.0
        + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
        + 0.00256 * math.cos(omega)
    )
    declination = math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long)))
    y = math.tan(math.radians(obliq) / 2.0) ** 2
    l0r = math.radians(l0)
    eq_time = 4.0 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * ecc * math.sin(mr)
        + 4 * ecc * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * ecc * ecc * math.sin(2 * mr)
    )
    true_solar_min = ((ts % 86400.0) / 60.0 + eq_time + 4.0 * longitude) % 1440.0
    hour_angle = math.radians(true_solar_min / 4.0 - 180.0)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)
    sin_zenith = math.sin(zenith)
    if sin_zenith < 1e-9:
        return 180.0, elevation
    cos_az = (math.sin(lat) * cos_zenith - math.sin(declination)) / (math.cos(lat) * sin_zenith)
    az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    azimuth = (az + 180.0) % 360.0 if hour_angle > 0 else (540.0 - az) % 360.0
    return azimuth, elevation


def hour_day(end_ts: float, tz) -> date:
    """Local date of the hour ending at end_ts (decided by the hour's midpoint)."""
    return datetime.fromtimestamp(end_ts - HOUR_S / 2, tz).date()


@dataclass(frozen=True)
class DayWeather:
    day: date
    hours: int
    t_mean: float
    hdd: float
    wind_mean: float
    ghi_kwh: float  # kWh/m2 on the Open-Meteo scale


def _mean(values: list) -> float:
    return sum(values) / len(values)


def day_weather(
    fc: Forecast, day: date, tz, min_hours: int = MIN_DAY_HOURS
) -> Optional[DayWeather]:
    """Daily aggregates for one local day, or None when the forecast does not cover enough of
    it. HDD is degree-days below HDD_BASE_C of the day's mean temperature."""
    temp, wind, ghi = [], [], []
    hours = 0
    for i, end in enumerate(fc.times):
        if hour_day(end, tz) != day:
            continue
        hours += 1
        if fc.temperature[i] is not None:
            temp.append(fc.temperature[i])
        if fc.wind[i] is not None:
            wind.append(fc.wind[i])
        if fc.ghi[i] is not None:
            ghi.append(fc.ghi[i])
    if min(len(temp), len(wind), len(ghi)) < min_hours:
        return None
    t_mean = _mean(temp)
    return DayWeather(
        day=day,
        hours=hours,
        t_mean=t_mean,
        hdd=max(0.0, HDD_BASE_C - t_mean),
        wind_mean=_mean(wind),
        ghi_kwh=sum(ghi) / 1000.0,
    )


def mean_ahead(
    fc: Forecast, series: tuple, from_ts: float, hours: int, min_share: float = 0.9
) -> Optional[float]:
    """Mean of a series over the `hours` hourly values from the hour that contains from_ts.
    None when the forecast does not reach that far, or when fewer than min_share of those
    values are known."""
    start = next((i for i, end in enumerate(fc.times) if end > from_ts), None)
    if start is None or start + hours > len(series):
        return None
    known = [v for v in series[start : start + hours] if v is not None]
    if len(known) < min_share * hours:
        return None
    return _mean(known)


def sun_hours(
    fc: Forecast,
    day: date,
    tz,
    latitude: float,
    longitude: float,
    az_min: float,
    az_max: float,
    ghi_min: float,
) -> list:
    """Start timestamps of the day's forecast hours whose irradiance reaches ghi_min while
    the sun stands inside [az_min, az_max] (the facade a zone's glass looks onto)."""
    out = []
    for i, end in enumerate(fc.times):
        g = fc.ghi[i]
        if g is None or g < ghi_min or hour_day(end, tz) != day:
            continue
        azimuth, elevation = solar_position(end - HOUR_S / 2, latitude, longitude)
        if elevation > 0 and az_min <= azimuth <= az_max:
            out.append(end - HOUR_S)
    return out
