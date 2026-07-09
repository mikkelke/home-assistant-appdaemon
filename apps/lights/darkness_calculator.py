"""
Darkness Calculator - rolling per-zone dark/bright classification (rewrite, 2026-06).

Contract - two independent questions:
  - "Is the room dark?"  -> answered HERE, from environment only (sun, outdoor lux,
    rain, indoor daylight). Computed rolling for every zone, 24/7. Occupancy is
    NEVER an input to the dark/bright decision or its hold timers.
  - "Do we need light?"  -> answered by the room light apps:
    occupied AND dark -> on; not occupied -> off; occupied AND bright -> off.

The whole decision, per zone (4 rules + asymmetric holds):
  1. always_dark zone (windowless)            -> DARK
  2. sun elevation < dusk_elevation (~3 deg)  -> DARK   (deterministic dusk/dawn envelope)
  3. smoothed outdoor lux < outdoor_dark      -> DARK   (clouds, rain, heavy overcast)
  4. smoothed outdoor lux > outdoor_bright
     AND indoor daylight > indoor_min_bright  -> BRIGHT (indoor check catches closed
                                                         blinds; lamp lux subtracted so
                                                         the lamp cannot feed back)
     ... but indoor daylight < indoor_min_bright * indoor_dark_fraction -> DARK
         (bright sky, dim room: blinds closed or sun not on this facade)
  otherwise                                   -> hold current state (true hysteresis)

Anti-flap:
  - Outdoor lux is a rolling median over ``outdoor_smoothing_seconds`` (~10 min):
    a sunbreak or a single dark cloud cannot move the decision.
  - Asymmetric minimum state age before the opposite flip commits:
      bright -> dark after ``hold_bright_to_dark_seconds`` (short: lamps on promptly)
      dark -> bright after ``hold_dark_to_bright_seconds`` (long: lamps off only when
      sustainably sun-bright), multiplied by ``rain_hold_multiplier`` while raining or
      within ``rain_grace_seconds`` after rain (sunbreaks during showers are ignored).
  - "Lamp daylight": if any configured zone light is on, ``light_on_lux_offset`` is
    subtracted from indoor lux before the BRIGHT check, so the lamp's own contribution
    can never classify the room as bright.

Publish contract (unchanged from the previous implementation):
  - ``binary_sensor.dark_<name>``  on/off        (confirmed only)
  - ``sensor.darkness_<name>``     dark|bright   (confirmed only)
  - ``sensor.room_state_<name>``   "Occupied|Empty (Dark|Bright)" labels; occupancy
    from ``presence_sensors`` per published name (label only - see contract above)
  - optional ``room_state_helpers`` input_text sync with the same label
  - mirrors republish the source zone classification under their own names
  - ``pending_target`` / ``pending_*`` attributes are informational only (a flip that
    is currently blocked by a hold); downstream apps must act on confirmed state.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import time
from collections import deque
from statistics import median

DARK = "dark"
BRIGHT = "bright"


class DarknessCalculator(hass.Hass):
    def initialize(self):
        a = self.args

        # Global signals
        self.outdoor_sensor = a.get("outdoor_sensor", "sensor.gw2000a_solar_lux")
        self.rain_sensor = a.get("rain_sensor")  # binary_sensor, on = raining
        self.sun_entity = a.get("sun_entity", "sun.sun")
        self.dusk_elevation = float(a.get("dusk_elevation", 3.0))

        # Smoothing and holds
        self.smoothing_s = float(a.get("outdoor_smoothing_seconds", 600))
        self.hold_bright_to_dark_s = float(a.get("hold_bright_to_dark_seconds", 180))
        self.hold_dark_to_bright_s = float(a.get("hold_dark_to_bright_seconds", 1200))
        self.rain_hold_multiplier = float(a.get("rain_hold_multiplier", 2.0))
        self.rain_grace_s = float(a.get("rain_grace_seconds", 1800))
        # Below indoor_min_bright * fraction the room is DARK even under a bright sky
        # (closed blinds / sun not on this facade); between fraction and 1.0 we hold.
        self.indoor_dark_fraction = float(a.get("indoor_dark_fraction", 0.6))

        # Gloomy sky (GLOBAL mechanism - one sky for the whole apartment): raining or
        # recently rained, OR heavy overcast (sun well up yet the sky far dimmer than
        # clear-sky could be). While gloomy, every zone's outdoor_dark is multiplied:
        # the apartment is sun-lit by design, so grey days feel dark well above the
        # dry (golden-hour) thresholds.
        self.gloomy_dark_multiplier = float(a.get("gloomy_dark_multiplier", 1.0))
        self.gloomy_overcast_min_elevation = float(a.get("gloomy_overcast_min_elevation", 20.0))
        self.gloomy_overcast_max_lux = float(a.get("gloomy_overcast_max_lux", 12000))
        # Indoor bright-bar scaling is part of the shared mechanism (zones may override).
        self.indoor_min_scales_default = bool(a.get("indoor_min_scales_with_outdoor", True))
        self.indoor_min_floor_fraction_default = float(a.get("indoor_min_floor_fraction", 0.25))

        self.debounce_s = float(a.get("sensor_debounce_seconds", 2.0))
        self.periodic_s = float(a.get("periodic_recompute_seconds", 90))

        # Publishing
        self.publish_binary_prefix = a.get("publish_binary_prefix", "binary_sensor.dark_")
        self.publish_sensor_prefix = a.get("publish_sensor_prefix", "sensor.darkness_")
        self.publish_room_state_prefix = a.get("publish_room_state_prefix", "sensor.room_state_")
        self.presence_sensors = a.get("presence_sensors", {})
        self.room_state_helpers = a.get("room_state_helpers") or {}

        self.zones = a.get("zones", {})

        # Caches (event-fed; the periodic recompute re-pulls as a safety net)
        self._indoor = {}            # sensor entity -> float lux
        self._outdoor_samples = deque()  # (ts, lux)
        self._outdoor_last = None
        self._outdoor_ts = None
        self._raining = False
        self._rain_ended_at = 0.0

        # Per-zone state machine
        self._state = {}             # zone -> DARK | BRIGHT
        self._state_since = {}       # zone -> ts of last confirmed flip
        self._pending = {}           # zone -> {"target", "since"} (hold-blocked flip)
        self._hold_timers = {}       # zone -> run_in handle
        self._debounce_timers = {}   # zone -> run_in handle
        self._publish_snapshots = {} # entity_id -> tuple

        self._seed_caches()
        self._restore_states()
        self._register_listeners()

        if self.periodic_s > 0:
            self._periodic_handle = self.run_every(
                lambda _: self._periodic(), self.datetime(), int(self.periodic_s)
            )
        else:
            self._periodic_handle = None
        self.run_in(lambda _: self._recompute_all(), 2)

        self.log(
            f"Darkness Calculator (streamlined): {len(self.zones)} zones, "
            f"outdoor={self.outdoor_sensor}, rain={self.rain_sensor}, "
            f"holds {self.hold_bright_to_dark_s:.0f}s/{self.hold_dark_to_bright_s:.0f}s "
            f"(x{self.rain_hold_multiplier:.1f} around rain), "
            f"smoothing {self.smoothing_s:.0f}s, gloomy x{self.gloomy_dark_multiplier:.1f} "
            f"(overcast: elev>={self.gloomy_overcast_min_elevation:.0f}deg & <{self.gloomy_overcast_max_lux:.0f}lx)",
            level="INFO",
        )

    def terminate(self):
        for h in list(self._hold_timers.values()) + list(self._debounce_timers.values()):
            self._cancel(h)
        self._hold_timers.clear()
        self._debounce_timers.clear()
        self._cancel(getattr(self, "_periodic_handle", None))

    # ─────────────────────────────────────────────────────────────
    # Setup
    # ─────────────────────────────────────────────────────────────

    def _zone_sensor_list(self, zcfg):
        sensors = zcfg.get("sensors")
        if isinstance(sensors, str):
            return [sensors]
        return list(sensors or [])

    def _zone_light_list(self, zcfg):
        lights = zcfg.get("lights")
        if isinstance(lights, str):
            return [lights]
        return list(lights or [])

    def _seed_caches(self):
        v = self._get_float(self.outdoor_sensor)
        if v is not None:
            self._outdoor_last = v
            self._outdoor_ts = time.time()
            self._outdoor_samples.append((time.time(), v))
        if self.rain_sensor:
            self._raining = self._get_raw(self.rain_sensor) == "on"
        for zcfg in self.zones.values():
            for s in self._zone_sensor_list(zcfg):
                val = self._get_float(s)
                if val is not None:
                    self._indoor[s] = val

    def _restore_states(self):
        """Resume the last published classification across restarts; first flip is never hold-blocked."""
        for zone in self.zones:
            prev = self._get_raw(f"{self.publish_sensor_prefix}{zone}")
            self._state[zone] = prev if prev in (DARK, BRIGHT) else DARK
            self._state_since[zone] = 0.0
            if prev in (DARK, BRIGHT):
                self.log(f"[{zone}] restored {prev} from HA", level="INFO")

    def _register_listeners(self):
        self.listen_state(self._on_outdoor, self.outdoor_sensor)
        if self.rain_sensor:
            self.listen_state(self._on_rain, self.rain_sensor)

        presence_entities = set()
        for zone, zcfg in self.zones.items():
            for s in self._zone_sensor_list(zcfg):
                self.listen_state(self._on_indoor, s, zone=zone)
            for ent in self._zone_light_list(zcfg):
                self.listen_state(self._on_zone_input, ent, zone=zone)
            covers = zcfg.get("covers")
            for ent in ([covers] if isinstance(covers, str) else (covers or [])):
                self.listen_state(self._on_zone_input, ent, zone=zone)
            for name in [zone] + list(zcfg.get("mirrors", [])):
                cfg = self.presence_sensors.get(name)
                ents = cfg if isinstance(cfg, (list, tuple)) else [cfg]
                presence_entities.update(e for e in ents if e)
        for ent in presence_entities:
            self.listen_state(self._on_presence, ent)

    # ─────────────────────────────────────────────────────────────
    # Signal helpers
    # ─────────────────────────────────────────────────────────────

    def _get_raw(self, entity):
        try:
            return self.get_state(entity)
        except Exception:
            return None

    def _get_float(self, entity):
        try:
            v = self.get_state(entity)
            return float(v) if v not in (None, "unknown", "unavailable", "") else None
        except Exception:
            return None

    def _sun_elevation(self):
        try:
            elev = self.get_state(self.sun_entity, attribute="elevation")
            return float(elev) if elev is not None else None
        except Exception:
            return None

    def _outdoor_smoothed(self):
        now = time.time()
        cutoff = now - self.smoothing_s
        while self._outdoor_samples and self._outdoor_samples[0][0] < cutoff:
            self._outdoor_samples.popleft()
        if self._outdoor_samples:
            return median(v for _, v in self._outdoor_samples)
        return self._outdoor_last if self._outdoor_last is not None else 0.0

    def _outdoor_valid(self):
        """False when the outdoor sensor has produced no usable value recently
        (entity unavailable / weather station offline). Missing outdoor must NEVER
        be treated as 0 lx - see the fallback branch in _decide."""
        if self._outdoor_samples:
            return True
        ts = self._outdoor_ts
        return ts is not None and (time.time() - ts) < max(900.0, self.smoothing_s)

    def _rain_active_or_recent(self):
        if self._raining:
            return True
        return (time.time() - self._rain_ended_at) < self.rain_grace_s

    def _gloomy(self, out=None, elev=None):
        """Shared-sky gloom: rain (or recent) OR heavy overcast (sun well up, sky dim)."""
        if self._rain_active_or_recent():
            return True, "rain"
        if elev is None:
            elev = self._sun_elevation()
        if out is None:
            out = self._outdoor_smoothed()
        if (
            self._outdoor_valid()
            and elev is not None
            and elev >= self.gloomy_overcast_min_elevation
            and out < self.gloomy_overcast_max_lux
        ):
            return True, "overcast"
        return False, None

    def _any_on(self, entities):
        for ent in entities:
            if self._get_raw(ent) == "on":
                return True
        return False

    def _zone_indoor(self, zone):
        vals = [self._indoor[s] for s in self._zone_sensor_list(self.zones[zone]) if s in self._indoor]
        return (sum(vals) / len(vals)) if vals else None

    def _zone_daylight(self, zone):
        """Indoor lux with the zone lamps' own contribution removed - feedback-proof."""
        indoor = self._zone_indoor(zone)
        if indoor is None:
            return None
        zcfg = self.zones[zone]
        offset = float(zcfg.get("light_on_lux_offset", 0) or 0)
        if offset > 0 and self._any_on(self._zone_light_list(zcfg)):
            return max(0.0, indoor - offset)
        return indoor

    def _any_presence_on(self, publish_name):
        cfg = self.presence_sensors.get(publish_name)
        if not cfg:
            return False
        ents = cfg if isinstance(cfg, (list, tuple)) else [cfg]
        return self._any_on(e for e in ents if e)

    # ─────────────────────────────────────────────────────────────
    # Core decision
    # ─────────────────────────────────────────────────────────────

    def _decide(self, zone):
        """Return (target, reason). target is DARK, BRIGHT, or None (= hold current)."""
        zcfg = self.zones[zone]
        if zcfg.get("always_dark"):
            return DARK, "always_dark zone"

        elev = self._sun_elevation()
        if elev is not None and elev < self.dusk_elevation:
            return DARK, f"sun {elev:.1f} deg < {self.dusk_elevation:.1f} deg"

        out = self._outdoor_smoothed()
        outdoor_dark = float(zcfg.get("outdoor_dark", 4000))
        outdoor_bright = float(zcfg.get("outdoor_bright", 10000))

        if not self._outdoor_valid():
            # Weather station offline: decide from the room's own daylight alone
            # (unscaled bar, same hysteresis band as the facade rule). Sun elevation
            # already forced DARK above; with no indoor signal either, hold.
            daylight = self._zone_daylight(zone)
            indoor_min = float(zcfg.get("indoor_min_bright", 0))
            if daylight is None or indoor_min <= 0:
                return None, "outdoor sensor unavailable, no indoor signal - holding"
            if daylight >= indoor_min:
                return BRIGHT, f"outdoor unavailable - indoor daylight {daylight:.0f}lx >= {indoor_min:.0f}lx (fallback)"
            if daylight < indoor_min * self.indoor_dark_fraction:
                return DARK, (
                    f"outdoor unavailable - indoor daylight {daylight:.0f}lx "
                    f"< {indoor_min * self.indoor_dark_fraction:.0f}lx (fallback)"
                )
            return None, f"outdoor unavailable - indoor daylight {daylight:.0f}lx in band - holding"

        # Gloomy sky raises every zone's dark threshold (global mechanism); capped
        # below outdoor_bright so the dark rule cannot swallow the bright band.
        gloomy, gloomy_why = self._gloomy(out=out, elev=elev)
        if gloomy and self.gloomy_dark_multiplier > 1.0:
            outdoor_dark = min(outdoor_dark * self.gloomy_dark_multiplier, outdoor_bright * 0.9)
        if out < outdoor_dark:
            gl = f" [gloomy: {gloomy_why}]" if gloomy else ""
            return DARK, f"outdoor {out:.0f}lx < {outdoor_dark:.0f}lx{gl}"

        if out > outdoor_bright:
            daylight = self._zone_daylight(zone)
            indoor_min = float(zcfg.get("indoor_min_bright", 0))
            if daylight is None:
                # No indoor data: trust outdoor alone.
                return BRIGHT, f"outdoor {out:.0f}lx > {outdoor_bright:.0f}lx (no indoor data)"
            # Scale the indoor "bright enough" bar to outdoor brightness (GLOBAL
            # mechanism, per-zone override): brighter outside -> less indoor daylight
            # needed for a shaded facade / closed blind to still count as not-dark;
            # floored so a genuinely dark room reads DARK.
            if zcfg.get("indoor_min_scales_with_outdoor", self.indoor_min_scales_default) and indoor_min > 0:
                _ff = float(zcfg.get("indoor_min_floor_fraction", self.indoor_min_floor_fraction_default))
                indoor_min = indoor_min * min(1.0, max(_ff, outdoor_bright / max(out, 1.0)))
            if daylight > indoor_min:
                return (
                    BRIGHT,
                    f"outdoor {out:.0f}lx > {outdoor_bright:.0f}lx, "
                    f"indoor daylight {daylight:.0f}lx > {indoor_min:.0f}lx",
                )
            # Bright sky but dim room (closed blinds / sun not on this facade yet):
            # below the dark fraction the room genuinely needs light; in between, hold.
            indoor_dark_floor = indoor_min * self.indoor_dark_fraction
            if daylight < indoor_dark_floor:
                return DARK, (
                    f"outdoor {out:.0f}lx bright but indoor daylight {daylight:.0f}lx "
                    f"< {indoor_dark_floor:.0f}lx (blinds/facade)"
                )
            return None, (
                f"outdoor {out:.0f}lx bright, indoor daylight {daylight:.0f}lx in "
                f"{indoor_dark_floor:.0f}-{indoor_min:.0f}lx band - holding"
            )

        return None, f"outdoor {out:.0f}lx in {outdoor_dark:.0f}-{outdoor_bright:.0f}lx band - holding"

    def _hold_needed(self, current, target):
        if current == BRIGHT and target == DARK:
            return self.hold_bright_to_dark_s
        need = self.hold_dark_to_bright_s
        if self._rain_active_or_recent():
            need *= self.rain_hold_multiplier
        return need

    def _recompute_zone(self, zone):
        try:
            target, reason = self._decide(zone)
        except Exception as e:
            self.log(f"[{zone}] decide error: {e}", level="ERROR")
            return

        now = time.time()
        current = self._state.get(zone, DARK)

        if target is None or target == current:
            if zone in self._pending:
                self._pending.pop(zone, None)
                self._cancel_hold_timer(zone)
        else:
            age = now - self._state_since.get(zone, 0.0)
            need = self._hold_needed(current, target)
            if age >= need:
                self._state[zone] = target
                self._state_since[zone] = now
                self._pending.pop(zone, None)
                self._cancel_hold_timer(zone)
                self.log(f"[{zone}] {current.upper()} -> {target.upper()} - {reason}", level="INFO")
            else:
                pend = self._pending.get(zone)
                if not pend or pend["target"] != target:
                    self._pending[zone] = {"target": target, "since": now}
                    self.log(
                        f"[{zone}] {target.upper()} blocked by hold "
                        f"({need - age:.0f}s remaining) - {reason}",
                        level="DEBUG",
                    )
                self._schedule_hold_timer(zone, need - age + 1.0)

        self._publish_zone(zone, reason)

    def _recompute_all(self):
        for zone in self.zones:
            self._recompute_zone(zone)

    def _periodic(self):
        """Safety net: re-pull signals (missed events cannot stick) and recompute."""
        v = self._get_float(self.outdoor_sensor)
        if v is not None:
            self._outdoor_last = v
            self._outdoor_ts = time.time()
            self._outdoor_samples.append((time.time(), v))
        if self.rain_sensor:
            raining = self._get_raw(self.rain_sensor) == "on"
            if self._raining and not raining:
                self._rain_ended_at = time.time()
            self._raining = raining
        for zcfg in self.zones.values():
            for s in self._zone_sensor_list(zcfg):
                val = self._get_float(s)
                if val is not None:
                    self._indoor[s] = val
        self._recompute_all()

    # ─────────────────────────────────────────────────────────────
    # Event handlers
    # ─────────────────────────────────────────────────────────────

    def _on_outdoor(self, entity, attribute, old, new, kwargs):
        try:
            v = float(new)
        except (TypeError, ValueError):
            return
        self._outdoor_last = v
        self._outdoor_ts = time.time()
        self._outdoor_samples.append((time.time(), v))
        self._debounced_recompute_all()

    def _on_rain(self, entity, attribute, old, new, kwargs):
        raining = new == "on"
        if self._raining and not raining:
            self._rain_ended_at = time.time()
        self._raining = raining
        self._debounced_recompute_all()

    def _on_indoor(self, entity, attribute, old, new, kwargs):
        try:
            self._indoor[entity] = float(new)
        except (TypeError, ValueError):
            return
        self._debounced_recompute_zone(kwargs.get("zone"))

    def _on_zone_input(self, entity, attribute, old, new, kwargs):
        """Zone light or cover changed - recompute so lamp offset / blind effect applies now."""
        self._debounced_recompute_zone(kwargs.get("zone"))

    def _on_presence(self, entity, attribute, old, new, kwargs):
        """Occupancy only changes room_state labels, never the dark/bright classification."""
        self.run_in(lambda _: self._recompute_all(), 0)

    def _debounced_recompute_zone(self, zone):
        if not zone:
            return
        self._cancel(self._debounce_timers.get(zone))
        self._debounce_timers[zone] = self.run_in(
            lambda _, z=zone: self._recompute_zone(z), self.debounce_s
        )

    def _debounced_recompute_all(self):
        self._cancel(self._debounce_timers.get("__all__"))
        self._debounce_timers["__all__"] = self.run_in(
            lambda _: self._recompute_all(), self.debounce_s
        )

    def _schedule_hold_timer(self, zone, delay_s):
        self._cancel_hold_timer(zone)
        self._hold_timers[zone] = self.run_in(
            lambda _, z=zone: self._recompute_zone(z), max(1.0, delay_s)
        )

    def _cancel_hold_timer(self, zone):
        self._cancel(self._hold_timers.pop(zone, None))

    def _cancel(self, handle):
        try:
            if handle and self.timer_running(handle):
                self.cancel_timer(handle)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # Publishing
    # ─────────────────────────────────────────────────────────────

    def _zone_attrs(self, zone, reason):
        zcfg = self.zones[zone]
        now = time.time()
        pend = self._pending.get(zone)
        pending_target = pend["target"] if pend else None
        pending_since = pend["since"] if pend else None
        remaining = None
        if pend:
            need = self._hold_needed(self._state.get(zone, DARK), pend["target"])
            remaining = max(0.0, need - (now - self._state_since.get(zone, 0.0)))

        indoor = self._zone_indoor(zone)
        daylight = self._zone_daylight(zone)
        out_smooth = self._outdoor_smoothed()
        gloomy, gloomy_why = self._gloomy(out=out_smooth)
        eff_dark = float(zcfg.get("outdoor_dark", 4000))
        _ob = float(zcfg.get("outdoor_bright", 10000))
        if gloomy and self.gloomy_dark_multiplier > 1.0:
            eff_dark = min(eff_dark * self.gloomy_dark_multiplier, _ob * 0.9)
        return {
            "indoor_lux": round(indoor, 1) if indoor is not None else None,
            "indoor_daylight_lux": round(daylight, 1) if daylight is not None else None,
            "outdoor_lux": round(self._outdoor_last, 1) if self._outdoor_last is not None else None,
            "outdoor_smoothed_lux": round(out_smooth, 1),
            "outdoor_valid": self._outdoor_valid(),
            "raining": self._raining,
            "rain_recent": self._rain_active_or_recent(),
            "gloomy": gloomy,
            "gloomy_reason": gloomy_why,
            "sun_elevation": self._sun_elevation(),
            "outdoor_dark": zcfg.get("outdoor_dark"),
            "outdoor_dark_effective": round(eff_dark),
            "outdoor_bright": zcfg.get("outdoor_bright"),
            "indoor_min_bright": zcfg.get("indoor_min_bright"),
            # Legacy keys kept for dashboards/diagnostics (now in outdoor lux terms):
            "dark_threshold": round(eff_dark),
            "bright_threshold": zcfg.get("outdoor_bright"),
            "day_type": "raining" if self._raining else "dry",
            "reason": reason,
            "last_change": self._state_since.get(zone) or None,
            "pending_target": pending_target,
            "pending_since": pending_since,
            "pending_remaining_seconds": round(remaining, 1) if remaining is not None else None,
            "source_zone": zone,
        }

    def _publish_zone(self, zone, reason):
        attrs = self._zone_attrs(zone, reason)
        is_dark = self._state.get(zone, DARK) == DARK
        names = [zone] + list(self.zones[zone].get("mirrors", []))
        for name in names:
            try:
                self._publish_one(name, is_dark, attrs)
            except Exception as e:
                self.log(f"Publish error [{name}]: {e}", level="ERROR")

    def _publish_one(self, name, is_dark, attrs):
        occupied = self._any_presence_on(name)
        label = (
            ("Occupied" if occupied else "Empty")
            + (" (Dark)" if is_dark else " (Bright)")
        )
        snap = (
            is_dark,
            occupied,
            attrs.get("pending_target"),
            round((attrs.get("indoor_lux") or 0), -1),
            round((attrs.get("outdoor_smoothed_lux") or 0), -2),
            attrs.get("raining"),
            attrs.get("gloomy"),
        )

        bin_ent = f"{self.publish_binary_prefix}{name}"
        sen_ent = f"{self.publish_sensor_prefix}{name}"
        room_ent = f"{self.publish_room_state_prefix}{name}"

        if self._publish_snapshots.get(bin_ent) != snap:
            self.set_state(bin_ent, state="on" if is_dark else "off", attributes=attrs)
            self.set_state(sen_ent, state=DARK if is_dark else BRIGHT, attributes=attrs)
            self._publish_snapshots[bin_ent] = snap

        room_attrs = {
            "room": name,
            "occupied": occupied,
            "dark": is_dark,
            "presence_ts": time.time(),
            **attrs,
        }
        if self._publish_snapshots.get(room_ent) != snap:
            self.set_state(room_ent, state=label, attributes=room_attrs)
            self._publish_snapshots[room_ent] = snap
        else:
            try:
                self.set_state(room_ent, attributes=room_attrs)
            except Exception:
                pass

        helper = self.room_state_helpers.get(name)
        if helper:
            self._sync_helper(helper, label)

    def _sync_helper(self, helper_entity, label):
        try:
            if self._get_raw(helper_entity) == label:
                return
            try:
                self.set_state(helper_entity, state=label)
            except Exception:
                self.call_service("input_text/set_value", entity_id=helper_entity, value=label)
        except Exception as e:
            self.log(f"helper sync failed [{helper_entity}]: {e}", level="WARNING")
