"""
Weather opening alert: rooftop rain + wind-driven window rain.
Publishes weather_opening_alert_* entities via set_state for dashboards (e.g. Home Pulse).
"""

from __future__ import annotations

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _parse_iso_datetime(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(val, str):
        s = val.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def wind_in_band(wind_deg: float, bearing: float, half_band: float) -> bool:
    """True if meteorological wind direction (deg) is within bearing +/- half_band (0-360 wrap)."""
    w = wind_deg % 360.0
    b = bearing % 360.0
    diff = (w - b + 360.0) % 360.0
    return diff <= half_band or diff >= 360.0 - half_band


def _area_label(area_key: str) -> str:
    return area_key.replace("_", " ").strip().title() if area_key else ""


def _wind_scalar_to_kmh(value: float, uom: Optional[str]) -> float:
    """Convert wind speed/gust to km/h from common HA unit_of_measurement strings."""
    if uom is None or not isinstance(uom, str):
        return value
    u = uom.lower().replace(" ", "")
    if "m/s" in u or u == "ms-1":
        return value * 3.6
    if "mph" in u or "mi/h" in u:
        return value * 1.60934
    if "knot" in u or u in ("kn", "kt"):
        return value * 1.852
    if "ft/s" in u or "fps" in u:
        return value * 1.09728
    if "km/h" in u or "kph" in u or u == "kmh":
        return value
    # Default: assume already km/h (GW2000A-style)
    return value


class WeatherOpeningAlert(hass.Hass):
    def initialize(self) -> None:
        self.log("WeatherOpeningAlert initializing...")

        self.rain_entity = self.args.get("rain_rate_entity", "sensor.gw2000a_rain_rate_piezo")
        self.wind_speed_entity = self.args.get("wind_speed_entity", "sensor.gw2000a_wind_speed")
        self.wind_gust_entity = self.args.get("wind_gust_entity", "sensor.gw2000a_wind_gust")
        self.wind_dir_entity = self.args.get("wind_direction_entity", "sensor.gw2000a_wind_direction")

        self.rain_min = float(self.args.get("rain_rate_min_mmh", 0.5))
        self.wind_speed_min = float(self.args.get("wind_speed_min_kmh", 5))
        self.wind_gust_min = float(self.args.get("wind_gust_min_kmh", 10))
        self.half_band = float(self.args.get("exposure_band_degrees", 30))

        self.rooftop_sustain_min = float(self.args.get("rooftop_rain_sustain_minutes", 2))
        self.rooftop_clear_min = float(self.args.get("rooftop_rain_clear_minutes", 1))
        self.window_sustain_min = float(self.args.get("window_rain_sustain_minutes", 4))
        self.window_clear_min = float(self.args.get("window_rain_clear_minutes", 2))
        self.opening_open_min = float(self.args.get("opening_open_minutes", 0))

        raw_rp = self.args.get("room_priority", [])
        self.room_priority: list[str] = list(raw_rp) if isinstance(raw_rp, list) else []

        raw_openings = self.args.get("openings", [])
        if not isinstance(raw_openings, list) or not raw_openings:
            self.log("Config error: openings must be a non-empty list", level="ERROR")
            return

        self.openings: list[dict[str, Any]] = []
        for o in raw_openings:
            if not isinstance(o, dict):
                continue
            eid = o.get("entity_id")
            if not eid:
                continue
            self.openings.append(
                {
                    "entity_id": eid,
                    "bearing": float(o.get("bearing", 0)),
                    "rooftop": bool(o.get("rooftop", False)),
                    "area": str(o.get("area", "unknown")),
                }
            )

        self.evaluate_debounce_s = float(self.args.get("evaluate_debounce_seconds", 5))

        # Debounce timestamps (UTC)
        self._rain_above_since: Optional[datetime] = None
        self._rain_below_since: Optional[datetime] = None
        self._opened_since: dict[str, datetime] = {}
        self._wind_ok_since: dict[str, datetime] = {}

        self._debounce_handle: Optional[str] = None

        # Rooftop alert stays on through brief rain dips until rain below threshold
        # for rooftop_rain_clear_minutes (see _evaluate).
        self._rooftop_latched: bool = False

        # Window clear hysteresis
        self._window_false_since: Optional[datetime] = None
        self._prev_window_shown: bool = False
        self._last_window_winner: Optional[dict[str, Any]] = None

        weather_entities = [
            self.rain_entity,
            self.wind_speed_entity,
            self.wind_gust_entity,
            self.wind_dir_entity,
        ]
        for eid in weather_entities:
            self.listen_state(self._on_change, eid)

        for o in self.openings:
            self.listen_state(self._on_change, o["entity_id"])

        self.run_every(self._run_evaluate, "now", 60)
        self.run_in(self._run_evaluate, 2)

        self.log(
            f"Started: {len(self.openings)} openings, debounce {self.evaluate_debounce_s}s, run_every 60s",
            level="INFO",
        )

    def _on_change(
        self,
        entity: str,
        attribute: Optional[str],
        old: str,
        new: str,
        kwargs: dict,
    ) -> None:
        self._schedule_evaluate()

    def _schedule_evaluate(self) -> None:
        if self._debounce_handle is not None:
            try:
                self.cancel_timer(self._debounce_handle)
            except Exception:
                pass
            self._debounce_handle = None
        self._debounce_handle = self.run_in(self._run_evaluate_debounced, self.evaluate_debounce_s)

    def _run_evaluate_debounced(self, kwargs: dict) -> None:
        self._debounce_handle = None
        self._run_evaluate(kwargs)

    def _run_evaluate(self, kwargs: Optional[dict] = None) -> None:
        self.create_task(self._evaluate())

    async def _get_lc(self, entity_id: str) -> Optional[datetime]:
        try:
            lc = await self.get_state(entity_id, attribute="last_changed")
            return _parse_iso_datetime(lc)
        except Exception:
            return None

    async def _parse_numeric_state(self, entity_id: str) -> Optional[float]:
        try:
            st = await self.get_state(entity_id)
        except Exception:
            return None
        if st in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(st)
        except (TypeError, ValueError):
            return None

    async def _wind_speed_kmh(self) -> Optional[float]:
        val = await self._parse_numeric_state(self.wind_speed_entity)
        if val is None:
            return None
        try:
            uom = await self.get_state(self.wind_speed_entity, attribute="unit_of_measurement")
        except Exception:
            uom = None
        return _wind_scalar_to_kmh(val, uom if isinstance(uom, str) else None)

    async def _wind_gust_kmh(self) -> Optional[float]:
        val = await self._parse_numeric_state(self.wind_gust_entity)
        if val is None:
            return None
        try:
            uom = await self.get_state(self.wind_gust_entity, attribute="unit_of_measurement")
        except Exception:
            uom = None
        return _wind_scalar_to_kmh(val, uom if isinstance(uom, str) else None)

    async def _evaluate(self) -> None:
        now = datetime.now(timezone.utc)

        rain_rate = await self._parse_numeric_state(self.rain_entity)
        wind_dir = await self._parse_numeric_state(self.wind_dir_entity)
        speed_kmh = await self._wind_speed_kmh()
        gust_kmh = await self._wind_gust_kmh()

        rain_ok_now = rain_rate is not None and rain_rate >= self.rain_min

        # --- Rain sustain / clear timestamps ---
        # Do not clear _rain_above_since on a brief dip below threshold; only reset the
        # rain episode after rain has stayed below rooftop_rain_clear_minutes.
        if rain_ok_now:
            if self._rain_above_since is None:
                self._rain_above_since = (await self._get_lc(self.rain_entity)) or now
            self._rain_below_since = None
        else:
            if self._rain_below_since is None:
                self._rain_below_since = now

        rain_below_long = (
            self._rain_below_since is not None
            and (now - self._rain_below_since) >= timedelta(minutes=self.rooftop_clear_min)
        )
        if not rain_ok_now and rain_below_long:
            self._rain_above_since = None

        rain_sustained_rooftop = (
            self._rain_above_since is not None
            and rain_ok_now
            and (now - self._rain_above_since) >= timedelta(minutes=self.rooftop_sustain_min)
        )

        speed_qualifies = speed_kmh is not None and speed_kmh >= self.wind_speed_min
        gust_qualifies = gust_kmh is not None and gust_kmh >= self.wind_gust_min
        wind_strong = speed_qualifies or gust_qualifies

        wind_dir_ok = wind_dir is not None

        # last_changed only used in seed max() when that sensor's value currently
        # satisfies the corresponding part of the wind condition (plan: no mis-seed).
        lc_dir = await self._get_lc(self.wind_dir_entity) if wind_dir_ok else None
        lc_sp = await self._get_lc(self.wind_speed_entity) if speed_qualifies else None
        lc_gu = await self._get_lc(self.wind_gust_entity) if gust_qualifies else None

        # --- Per-opening open timestamps ---
        any_rooftop_open = False
        for o in self.openings:
            eid = o["entity_id"]
            try:
                st = await self.get_state(eid)
            except Exception:
                st = None
            open_now = st == "on"
            if o["rooftop"] and open_now:
                any_rooftop_open = True

            if open_now:
                if eid not in self._opened_since:
                    self._opened_since[eid] = (await self._get_lc(eid)) or now
            else:
                self._opened_since.pop(eid, None)
                self._wind_ok_since.pop(eid, None)

        # --- Wind-in-band per window (non-rooftop) ---
        for o in self.openings:
            if o["rooftop"]:
                continue
            eid = o["entity_id"]
            bearing = o["bearing"]
            in_band = wind_dir_ok and wind_in_band(wind_dir, bearing, self.half_band)
            w_ok = in_band and wind_strong
            if w_ok:
                if eid not in self._wind_ok_since:
                    seed_times: list[datetime] = []
                    if in_band and lc_dir is not None:
                        seed_times.append(lc_dir)
                    if speed_qualifies and lc_sp is not None:
                        seed_times.append(lc_sp)
                    if gust_qualifies and lc_gu is not None:
                        seed_times.append(lc_gu)
                    self._wind_ok_since[eid] = max(seed_times) if seed_times else now
            else:
                self._wind_ok_since.pop(eid, None)

        # --- Rooftop rain (latch: brief rain dip does not clear until rain_below_long) ---
        if not any_rooftop_open:
            self._rooftop_latched = False
        elif rain_below_long:
            self._rooftop_latched = False
        elif rain_ok_now and any_rooftop_open and (
            self._rooftop_latched or rain_sustained_rooftop
        ):
            self._rooftop_latched = True
        elif not rain_ok_now:
            pass  # keep _rooftop_latched during dip shorter than rooftop_clear_min
        else:
            self._rooftop_latched = False

        rooftop_active = self._rooftop_latched

        rooftop_result = {
            "active": rooftop_active,
            "reason": "Rain is falling and a rooftop door is open.",
            "target_area": "rooftop",
        }

        # --- Window rain qualifiers ---
        qualifiers: list[dict[str, Any]] = []
        if (
            rain_ok_now
            and self._rain_above_since is not None
            and wind_dir_ok
            and wind_strong
        ):
            for o in self.openings:
                if o["rooftop"]:
                    continue
                eid = o["entity_id"]
                try:
                    st = await self.get_state(eid)
                except Exception:
                    st = None
                if st != "on":
                    continue
                if not wind_in_band(wind_dir, o["bearing"], self.half_band):
                    continue

                t_open = self._opened_since.get(eid)
                t_rain = self._rain_above_since
                t_wind = self._wind_ok_since.get(eid)
                if t_open is None or t_rain is None or t_wind is None:
                    continue

                open_ready = t_open + timedelta(minutes=self.opening_open_min)
                combined_start = max(open_ready, t_rain, t_wind)
                if now < combined_start:
                    continue
                if (now - combined_start) < timedelta(minutes=self.window_sustain_min):
                    continue

                qualifiers.append(
                    {
                        "entity_id": eid,
                        "area": o["area"],
                        "open_since": t_open,
                        "combined_start": combined_start,
                    }
                )

        def sort_key(q: dict[str, Any]) -> tuple:
            pri = self.room_priority.index(q["area"]) if q["area"] in self.room_priority else 999
            return (q["open_since"], pri)

        window_raw = False
        winner: Optional[dict[str, Any]] = None
        if qualifiers:
            qualifiers.sort(key=sort_key)
            winner = qualifiers[0]
            window_raw = True

        if window_raw and winner is not None:
            self._last_window_winner = winner

        window_clear_td = timedelta(minutes=self.window_clear_min)
        if window_raw:
            self._window_false_since = None
            window_show = True
        else:
            if self._prev_window_shown:
                if self._window_false_since is None:
                    self._window_false_since = now
                window_show = (now - self._window_false_since) < window_clear_td
            else:
                self._window_false_since = None
                window_show = False

        if not window_show:
            self._last_window_winner = None

        self._prev_window_shown = window_show

        win_area = ""
        win_reason = ""
        if window_show and self._last_window_winner:
            win_area = self._last_window_winner["area"]
            label = _area_label(win_area)
            win_reason = f"Rain and wind are hitting the {label} window."

        window_result = {
            "active": window_show,
            "reason": win_reason,
            "target_area": win_area,
        }

        priority = self._eval_priority(rooftop_result, window_result)

        await self._publish(priority, rooftop_result, window_result)

    def _eval_priority(
        self,
        rooftop: dict[str, Any],
        window: dict[str, Any],
    ) -> dict[str, Any]:
        if rooftop.get("active"):
            return {
                "priority": "rooftop_rain",
                "reason": rooftop["reason"],
                "target_area": rooftop["target_area"],
            }
        if window.get("active"):
            return {
                "priority": "window_rain",
                "reason": window["reason"],
                "target_area": window["target_area"],
            }
        return {"priority": "none", "reason": "", "target_area": ""}

    async def _publish(
        self,
        priority: dict[str, Any],
        rooftop: dict[str, Any],
        window: dict[str, Any],
    ) -> None:
        prio = priority["priority"]
        reason = priority["reason"]
        target = priority["target_area"]
        any_active = prio != "none"

        common = {"friendly_name": "Weather opening alert"}

        self.set_state(
            "binary_sensor.weather_opening_alert_active",
            state="on" if any_active else "off",
            attributes={
                **common,
                "device_class": "problem",
            },
        )
        self.set_state(
            "sensor.weather_opening_alert_priority",
            state=prio,
            attributes={**common},
        )
        self.set_state(
            "sensor.weather_opening_alert_reason",
            state=reason,
            attributes={**common},
        )
        self.set_state(
            "sensor.weather_opening_alert_target_area",
            state=target,
            attributes={**common},
        )

        self.set_state(
            "binary_sensor.weather_opening_alert_rooftop_rain",
            state="on" if rooftop.get("active") else "off",
            attributes={"friendly_name": "Weather opening alert - rooftop rain"},
        )
        self.set_state(
            "binary_sensor.weather_opening_alert_window_rain",
            state="on" if window.get("active") else "off",
            attributes={"friendly_name": "Weather opening alert - window rain"},
        )
        wr = window.get("reason", "") if window.get("active") else ""
        self.set_state(
            "sensor.weather_opening_alert_window_rain_reason",
            state=wr,
            attributes={"friendly_name": "Weather opening alert - window rain reason"},
        )
