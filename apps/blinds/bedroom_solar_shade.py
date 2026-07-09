"""
Bedroom Solar Shade: position the bedroom blind to block direct morning sun (heat)
while keeping the room bright enough that the lights aren't needed -- in collaboration
with the morning alarm.

Context:
  * Bedroom window faces ENE (~70 deg) -> only direct sun in the morning.
  * The wake routine (wakeup_bedroom.py) opens the blind to 38% at the alarm time
    (input_datetime.wakeup_bedroom). 38 is the user's PRIVACY FLOOR -- never go more open.
  * cover.bedroom_blind: 0 = open, 100 = closed (closed_is_100).
  * sensor.bedroom_presence_illuminance (lx) is the "enough light" feedback.

Logic each tick:
  * Inactive while the sun is down, before wake (alarm time + grace), or while asleep
    (input_boolean.mikkel_sleep_mode) -- leaves the night/wake blind to the routine.
  * No direct sun on the window -> open to the privacy floor (38) for maximum daylight.
  * Direct morning sun + bright -> close ABOVE 38 to block the beam, but only as far as the
    indoor illuminance stays above min_lux (feedback): too dim -> open a step toward 38;
    plenty of light -> close a step toward the max-shade cap to block more heat.
  * Respects manual/remote moves (pauses manual_pause_min) so it never fights bedroom_blind_control.

Opt-in via input_boolean.bedroom_solar_shade (OFF by default). Publishes sensor.bedroom_solar_shade_status.

HA helpers (via MCP): input_boolean.bedroom_solar_shade, input_number.bedroom_solar_shade_position (max-shade cap).
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import time, timedelta


class BedroomSolarShade(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.cover = a("cover_entity", "cover.bedroom_blind")
        self.sun = a("sun_entity", "sun.sun")
        self.radiation_sensor = a("radiation_sensor", "sensor.gw2000a_solar_radiation")
        self.illuminance_sensor = a("illuminance_sensor", "sensor.bedroom_presence_illuminance")
        self.enable_entity = a("enable_entity", "input_boolean.bedroom_solar_shade")
        self.position_entity = a("position_entity", "input_number.bedroom_solar_shade_position")
        # morning-alarm collaboration
        self.alarm_time_entity = a("alarm_time_entity", "input_datetime.wakeup_bedroom")
        self.sleep_entity = a("sleep_entity", "input_boolean.mikkel_sleep_mode")
        self.wake_grace_min = int(a("wake_grace_min", 20))
        self.fallback_wake = self._parse_hhmm(a("fallback_wake", "07:30"), time(7, 30))
        # geometry / thresholds
        self.window_az = float(a("window_azimuth", 70))
        self.az_tol = float(a("az_tolerance", 55))
        self.min_elev = float(a("min_elevation", 3))
        self.rad_thr = float(a("radiation_threshold", 250))
        self.min_lux = float(a("min_lux", 200))
        self.lux_high_factor = float(a("lux_high_factor", 1.3))
        self.step = int(a("step", 9))
        self.open_pos = int(a("open_position", 38))   # privacy floor = wake-routine target
        self.max_pos = int(a("max_position", 100))
        self.default_shade = int(a("default_shade_position", 55))  # legacy fallback (unused when max_shade set)
        self.max_shade = int(a("max_shade", 92))  # hard cap only; the illuminance feedback is the real limiter
        self.manual_pause_min = int(a("manual_pause_min", 120))
        self.pos_tol = int(a("position_tolerance", 6))
        self.interval_min = int(a("check_interval_min", 10))
        self.status_entity = a("status_entity", "sensor.bedroom_solar_shade_status")
        self.dry_run = bool(a("dry_run", False))

        self._last_cmd = None
        self._override_until = None

        self.listen_state(self._on_change, self.enable_entity)
        self.listen_state(self._on_change, self.position_entity)
        self.listen_state(self._on_change, self.sleep_entity)
        self.listen_state(self._on_change, self.sun, attribute="azimuth")
        self.listen_state(self._on_cover_change, self.cover, attribute="current_position")
        self.run_every(self._tick, "now", self.interval_min * 60)
        self.log(f"BedroomSolarShade started (dry_run={self.dry_run}, open/floor={self.open_pos}, window_az={self.window_az})")

    # ---------- helpers ----------
    def _parse_hhmm(self, s, fallback):
        try:
            p = str(s).split(":")
            return time(int(p[0]), int(p[1]))
        except (TypeError, ValueError, IndexError):
            return fallback

    def _add_min(self, t, m):
        total = (t.hour * 60 + t.minute + m) % 1440
        return time(total // 60, total % 60)

    def _num(self, entity, default):
        try:
            return float(self.get_state(entity))
        except (TypeError, ValueError):
            return default

    def _num_attr(self, entity, attr, default):
        try:
            v = self.get_state(entity, attribute=attr)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _on_change(self, entity, attribute, old, new, kwargs):
        self.run_in(self._tick, 1)

    def _on_cover_change(self, entity, attribute, old, new, kwargs):
        try:
            pos = int(float(new))
        except (TypeError, ValueError):
            return
        if self._last_cmd is not None and abs(pos - self._last_cmd) > self.pos_tol:
            self._override_until = self.get_now() + timedelta(minutes=self.manual_pause_min)
            self.log(f"Manual blind move to {pos}% -> pause shading {self.manual_pause_min} min")

    # ---------- main ----------
    def _tick(self, kwargs=None):
        if self.get_state(self.enable_entity) != "on":
            self._publish("disabled", "Sun shade off", {})
            return

        now = self.get_now()
        elev = self._num_attr(self.sun, "elevation", -90.0)
        az = self._num_attr(self.sun, "azimuth", 0.0)
        if elev <= self.min_elev:
            self._publish("inactive", "Sun down", {"elevation": round(elev, 1)})
            return

        # Collaborate with the morning alarm: stay out of it until you're actually up.
        wake = self._parse_hhmm(self.get_state(self.alarm_time_entity), self.fallback_wake)
        active_after = self._add_min(wake, self.wake_grace_min)
        asleep = self.get_state(self.sleep_entity) == "on"
        if asleep or now.time() < active_after:
            self._publish(
                "waiting_wake",
                f"Leaving the blind to the wake routine (wake {wake.strftime('%H:%M')}, asleep={asleep})",
                {"wake": wake.strftime("%H:%M")},
            )
            return
        if self._override_until is not None and now < self._override_until:
            self._publish("manual", f"Paused after a manual move until {self._override_until.strftime('%H:%M')}", {})
            return

        rad = self._num(self.radiation_sensor, 0.0)
        on_window = abs(((az - self.window_az + 180) % 360) - 180) <= self.az_tol
        heat_risk = on_window and rad >= self.rad_thr
        max_shade = self.max_shade  # auto: close as far as the room stays bright enough (>= min_lux)
        cur = self._num_attr(self.cover, "current_position", None)
        lux = self._num(self.illuminance_sensor, -1.0)

        if not heat_risk:
            desired = self.open_pos
            reason = f"Open to {self.open_pos}% for daylight - sun off window (az {az:.0f} deg)"
        else:
            base = int(cur) if cur is not None else max_shade
            base = max(self.open_pos, min(max_shade, base))  # stay within the shade band [floor, cap]
            if lux < 0:
                desired, reason = max_shade, f"Shading to {max_shade}% (no lux reading)"
            elif lux < self.min_lux:
                desired = max(self.open_pos, base - self.step)
                reason = f"Opening to {desired}% - room dim ({lux:.0f} < {self.min_lux:.0f} lx)"
            elif lux > self.min_lux * self.lux_high_factor and base < max_shade:
                desired = min(max_shade, base + self.step)
                reason = f"Shading to {desired}% - bright ({lux:.0f} lx), blocking sun heat"
            else:
                desired = base
                reason = f"Holding {desired}% - balanced ({lux:.0f} lx, sun on window)"

        desired = max(self.open_pos, min(self.max_pos, int(desired)))
        self._publish(
            "shading" if heat_risk else "open", reason,
            {"desired": desired, "current": cur, "azimuth": round(az, 1), "elevation": round(elev, 1),
             "radiation": round(rad, 0), "illuminance": round(lux, 0) if lux >= 0 else None,
             "max_shade": max_shade, "min_lux": self.min_lux, "open_floor": self.open_pos,
             "wake": wake.strftime("%H:%M"), "on_window": on_window, "dry_run": self.dry_run},
        )

        if cur is not None and abs(cur - desired) <= self.pos_tol:
            return
        if self.dry_run:
            self.log(f"DRY-RUN would set {self.cover} -> {desired}% ({reason})")
            return
        self.call_service("cover/set_cover_position", entity_id=self.cover, position=desired)
        self._last_cmd = desired
        self.log(f"Set {self.cover} -> {desired}% ({reason})")

    def _publish(self, state, reason, attrs):
        a = dict(attrs or {})
        a["reason"] = reason
        a["friendly_name"] = "Bedroom sun shade"
        a["icon"] = "mdi:blinds-horizontal"
        try:
            self.set_state(self.status_entity, state=state, attributes=a)
        except Exception as e:
            self.log(f"publish failed: {e}", level="WARNING")
