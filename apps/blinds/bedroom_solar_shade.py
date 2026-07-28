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

POSITIONS ARE INVERTED vs Home Assistant's convention - read this before debugging.
cover.bedroom_blind ("Bedroom curtain") is a BOTTOM-UP blackout curtain: dark textile
inside the window travelling from the bottom edge upward. Fully extended (covered) = 100,
retracted (window clear) = 0. HA's convention is the opposite and it derives is_closed from
"position == 0", so while the curtain is fully drawn HA reports state: "open",
current_position: 100. Every number in this app - the 38 privacy floor, "close a step",
max-shade cap - is in the DEVICE's frame: higher = more covered. Never reason about this
curtain from the state string; use current_position and direction of travel.
Since 2026-07-28 a template entity cover.bedroom_curtain presents the TRUE orientation
(0 = covered = "closed") for humans, dashboards and voice - this app deliberately keeps
commanding the raw entity, because that is the frame every number here is written in.
See the banner in bedroom_blind_control.yaml for the incident this cost.

Restart-safe (2026-07-27): the manual-pause deadline and the baseline position used to
detect a manual move are persisted to bedroom_solar_shade_state.json and reloaded at
init, so a deploy during shading hours can't immediately re-command a hand-set blind, and
a move right after a restart is still classified as manual even before this app has
issued a command of its own this "session" (baseline seeded from the cover's current
position when nothing is persisted yet).

Opt-in via input_boolean.bedroom_solar_shade (OFF by default). Publishes sensor.bedroom_solar_shade_status.

HA helpers (via MCP): input_boolean.bedroom_solar_shade, input_number.bedroom_solar_shade_position (max-shade cap).

Away override (user, 2026-07-15): nobody home means nobody cares about keeping the room bright -
the daylight-preserving partial position only matters to someone actually in it. So while every
tracked person is away, skip the lux-balanced logic and just close fully (max_pos) to block the
most heat. Manual moves still win (checked first); the wake routine's own blind command does not
apply while away either, since there's nobody to wake up.
"""

import json
import os

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
from datetime import datetime, time, timedelta


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
        self.person_entities = a("person_entities", ["person.mikkel", "person.kristine"])
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
        self.state_file = a("state_file", "/conf/apps/blinds/bedroom_solar_shade_state.json")

        # Seconds of position-report silence that mean the motor has stopped. Must exceed
        # the ~5 s it leaves between steps while travelling (measured 2026-07-27), and stay
        # well under any sensible manual_pause_min.
        self.manual_settle_s = float(a("manual_settle_seconds", 12))

        self._last_cmd = None
        self._override_until = None
        # Manual-move episode tracking - see _on_cover_change / _manual_move_settled.
        self._manual_settle_handle = None
        self._manual_from_pos = None
        self._load_state()
        if self._last_cmd is None:
            # No persisted baseline (fresh install, or nothing commanded yet since the
            # last restart) - seed from the cover's CURRENT position so a manual move
            # right after a restart is still classified as manual instead of needing
            # this app to issue one command of its own first (deploy-during-shading bug,
            # 2026-07-27).
            seed = self._num_attr(self.cover, "current_position", None)
            if seed is not None:
                self._last_cmd = int(seed)
                self.log(f"No persisted last_cmd - seeded from current position {self._last_cmd}%")

        self.listen_state(self._on_change, self.enable_entity)
        self.listen_state(self._on_change, self.position_entity)
        self.listen_state(self._on_change, self.sleep_entity)
        self.listen_state(self._on_change, self.sun, attribute="azimuth")
        self.listen_state(self._on_cover_change, self.cover, attribute="current_position")
        # "now" fires at now+interval per AppDaemon's docs, not immediately - "immediate" is the real keyword; see 8666460.
        self.run_every(self._tick, "immediate", self.interval_min * 60)
        self.log(f"BedroomSolarShade started (dry_run={self.dry_run}, open/floor={self.open_pos}, window_az={self.window_az})")

    # -- state persistence (manual-pause override + baseline position; survives AD
    # restarts/deploys - see module docstring) ---------------------------------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
        except Exception:
            return
        lc = d.get("last_cmd")
        if lc is not None:
            try:
                self._last_cmd = int(lc)
            except (TypeError, ValueError):
                pass
        ou = d.get("override_until")
        if ou:
            try:
                self._override_until = datetime.fromisoformat(ou)
            except (TypeError, ValueError):
                pass

    def _save_state(self):
        try:
            data = {
                "last_cmd": self._last_cmd,
                "override_until": self._override_until.isoformat() if self._override_until else None,
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed ({e}) - continuing in-memory", level="WARNING")

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

    def _everyone_away(self):
        # Fail-safe: an unknown/unavailable tracker (dead phone, lost GPS fix) must NOT count as
        # away -- only a definite "somewhere that isn't home" reading does.
        for p in self.person_entities:
            state = self.get_state(p)
            if state in (None, "unknown", "unavailable", "home"):
                return False
        return True

    def _on_cover_change(self, entity, attribute, old, new, kwargs):
        """One hand/remote move is ONE manual move, however many position reports it emits.

        This motor reports its position roughly every 5 s while travelling, so the single
        close on 2026-07-27 22:28 (38% -> 100%, 37 s) arrived as SEVEN separate callbacks
        and produced seven identical "pause shading 120 min" log lines and seven state
        writes. The pause itself was right - it just said so seven times.

        So: push the pause out on every step (it must run from the END of the travel, not
        the start), persist immediately on the first step so an AppDaemon restart mid-travel
        still knows a manual move happened, then log exactly once when the motor settles."""
        try:
            pos = int(float(new))
        except (TypeError, ValueError):
            return
        if self._last_cmd is None or abs(pos - self._last_cmd) <= self.pos_tol:
            return

        now = self.get_now()
        self._override_until = now + timedelta(minutes=self.manual_pause_min)

        if self._manual_settle_handle is None:
            # First step of a new episode: remember where it started and persist now.
            self._manual_from_pos = self._last_cmd
            self._save_state()
        else:
            self._safe_cancel_timer(self._manual_settle_handle)

        try:
            self._manual_settle_handle = self.run_in(self._manual_move_settled, self.manual_settle_s, pos=pos)
        except Exception as e:
            # Never let the settle timer swallow the move: fall back to the old
            # log-immediately behaviour rather than losing the record entirely.
            self._manual_settle_handle = None
            self._save_state()
            self.log(
                f"Manual blind move to {pos}% -> pause shading {self.manual_pause_min} min "
                f"(settle timer failed: {e})",
                level="WARNING",
            )

    def _manual_move_settled(self, kwargs):
        """The motor stopped reporting for manual_settle_s - the move is over."""
        self._manual_settle_handle = None
        pos = kwargs.get("pos")
        frm = self._manual_from_pos
        self._manual_from_pos = None
        self._save_state()  # persist the final (latest) override_until
        span = f"{frm}% -> {pos}%" if frm is not None else f"to {pos}%"
        until = self._override_until.strftime("%H:%M") if self._override_until else "?"
        self.log(f"Manual blind move {span} -> pause shading {self.manual_pause_min} min (until {until})")

    def _safe_cancel_timer(self, handle):
        """Cancel a timer only if still running (avoids invalid-handle warnings)."""
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass

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

        if self._everyone_away():
            desired = self.max_pos
            reason = "Nobody home - closing fully to block the most heat"
            cur = self._num_attr(self.cover, "current_position", None)
            self._publish("away", reason, {"desired": desired, "current": cur})
            if cur is None or abs(cur - desired) > self.pos_tol:
                if self.dry_run:
                    self.log(f"DRY-RUN would set {self.cover} -> {desired}% ({reason})")
                else:
                    self.call_service("cover/set_cover_position", entity_id=self.cover, position=desired)
                    self._last_cmd = desired
                    self._save_state()
                    self.log(f"Set {self.cover} -> {desired}% ({reason})")
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
        self._save_state()
        self.log(f"Set {self.cover} -> {desired}% ({reason})")

    def _report_house_event(self, cause, effect):
        """Explain a shade decision to the dashboard's Home activity feed. Fire-and-forget:
        HouseEvents (apps/home_pulse) listens; if absent the event evaporates. audience=admin:
        Mikkel's bedroom blind - not the housemates' business."""
        try:
            self.fire_event("house_events_report", cause=cause, effect=effect, icon="mdi:blinds", audience="admin")
        except Exception:
            pass

    def _publish(self, state, reason, attrs):
        # Home-activity reporting on MODE TRANSITIONS only - this eval runs every few
        # minutes and nudges the position by one step at a time, so per-move reporting
        # would flood the feed. Entering/leaving shading (or closing for away) is the
        # decision a housemate actually notices. Never reported in dry-run - nothing moved.
        prev_mode = getattr(self, "_house_evt_mode", None)
        if state in ("open", "shading", "away"):
            if prev_mode is not None and state != prev_mode and not self.dry_run:
                if state == "shading":
                    self._report_house_event("Sun on the bedroom window", "Shading the bedroom blind")
                elif state == "away":
                    self._report_house_event("Nobody home", "Closing bedroom blind fully to block heat")
                elif state == "open" and prev_mode == "shading":
                    self._report_house_event("Sun moved off the window", "Opening bedroom blind for daylight")
            self._house_evt_mode = state
        a = dict(attrs or {})
        a["reason"] = reason
        a["friendly_name"] = "Bedroom sun shade"
        a["icon"] = "mdi:blinds-horizontal"
        try:
            # dry_run/on_window/radiation/illuminance silently drop from published attributes
            # whenever they're False/0 (all common at night or with dry_run on; not live-confirmed
            # 2026-07-15 since the feature was switched off at check time -- code-reasoned) --
            # AppDaemon 4.5.13 set_state bug, not ours; see smart_cooling.py's _publish() for details.
            self.set_state(self.status_entity, state=state, attributes=a, replace=True)
        except Exception as e:
            self.log(f"publish failed: {e}", level="WARNING")
