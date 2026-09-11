"""
RoomFeel - publishes sensor.<room>_feel per room: the household's single "how does this
room feel" reality, fused from the sensors that actually measure air where people sit,
with slab (floor) sensors, the kitchen ceiling unit and window-side units treated for what
they physically are (see sensor-placement memory, 2026-09-11).

Physical facts this fusion respects:
  - Salus wall thermostats (climate.<room>_thermostat, ~1.5 m) and FP300 mmWave presence
    sensors (sensor.<room>_presence_temperature/_humidity) are the AIR sources - equal
    weight, fused by median. Kristine's room has no FP300; several rooms have no ceiling
    or ceiling-adjacent ambient sensor at all.
  - kitchen/dining_room/living_room/hallway share ONE physical Salus (the hallway master,
    climate.family_room_thermostat) - their per-room sensor.<room>_temperature/_humidity
    clones are template duplicates of that same reading and are never read here. hallway
    has no FP300 either, so the family Salus is its sole/primary source; for
    kitchen/dining_room/living_room it is a corroborating (equally-weighted) vote next to
    that zone's own FP300.
  - Floor thermometers (SNZB-02P) are SLAB sensors, not air: 0.5-1.5 C below the wall
    units with a multi-hour lag. Never a primary vote - only a `floor+0.5` backup estimate
    when no air source reads at all, and the published `floor_spread_c` (air - floor)
    heating-season signal.
  - The kitchen Twinguard ceiling unit is a THIRD source for kitchen only, offset-corrected
    (it reads warm/dry vs the wall units) and never used alone.
  - kitchen/dining_room FP300s sit window-adjacent (sun/draft biased); when the rooftop
    solar sensor is over threshold during that room's own morning window, that FP300 is
    dropped from the median as long as another air source still reads.

Publish contract: one direction, no consumers read this app's own output back in. Booleans
(window_open/sun_hit/airing_helps/stale) are published as the literal strings "true"/"false"
rather than Python bools - AppDaemon 4.5.13's set_state silently drops an attribute whose
value is exactly False (see room_active.py's module docstring for the reference incident);
`sources`/`excluded` use a ["<none>"] sentinel rather than [] for the same reason. Optional
numeric attributes (floor_temp_c etc.) are still passed as plain None on a missing reading,
matching bedroom_comfort.py's precedent - a real 0.0/None reading is rare enough there that
working around the same set_state quirk for every numeric field is not worth it, and the
numbers rule below is the real safety net: nothing here is ever a fabricated 0.

Numbers rule: unavailable/unknown/non-numeric readings are excluded with a reason and never
enter the median as a 0. A room left with zero readable air source falls back to
floor+0.5 C; a room with neither air nor floor readable publishes state "unavailable"
(still not a fake number).

Also publishes two renamed single-source readings via the same fusion path:
  - sensor.bedroom_supply_air_temperature: straight mirror of
    sensor.ventilation_thermometer_temperature (a floor-type sensor sitting inside the
    bedroom's supply-air duct - it measures duct air, not the room, hence the rename).
  - sensor.technical_room_feel: the technical room (walled in by both bathrooms and the
    bedroom) has no FP300/Salus/floor of its own - its only reading is the Ecowitt
    gateway's own indoor channel, run through the same per-room pipeline as a one-source
    room entry (see room_feel.yaml's `technical_room`).
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime, time as dtime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

from climate_model import dew_point_c

_COMFORT_DETAIL = {
    "cold": "Cold — heating should catch up",
    "cool": "A bit cool",
    "comfortable": "Nothing to do",
    "warm": "A bit warm",
    "hot": "Hot — shade or airing helps",
}


def median(values):
    """Median of the non-None numbers in `values`; None when nothing is left."""
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def abs_humidity_g_m3(t_c, rh_pct):
    """Absolute humidity (g/m^3) from temperature (C) and relative humidity (%), via the
    Buck/Vaisala saturation-vapor-pressure approximation. None on invalid input."""
    try:
        t = float(t_c)
        rh = float(rh_pct)
    except (TypeError, ValueError):
        return None
    if rh <= 0 or rh > 100:
        return None
    es = 6.112 * math.exp((17.67 * t) / (t + 243.5))
    return 216.7 * (rh / 100.0 * es) / (273.15 + t)


def comfort_word(t_c, cold_max, cool_max, comfortable_max, warm_max):
    """Plain-English comfort band. None -> 'unknown'."""
    if t_c is None:
        return "unknown"
    if t_c < cold_max:
        return "cold"
    if t_c < cool_max:
        return "cool"
    if t_c < comfortable_max:
        return "comfortable"
    if t_c < warm_max:
        return "warm"
    return "hot"


def band_from_iaq(aqi, fresh_max, good_max, stuffy_max):
    """Twinguard IAQ index -> fresh/good/stuffy/poor. None -> 'unknown'."""
    if aqi is None:
        return "unknown"
    if aqi <= fresh_max:
        return "fresh"
    if aqi <= good_max:
        return "good"
    if aqi <= stuffy_max:
        return "stuffy"
    return "poor"


def band_from_eco2(eco2, fresh_max, good_max, stuffy_max):
    """Twinguard eCO2 (ppm) -> fresh/good/stuffy/poor. None -> 'unknown'."""
    if eco2 is None:
        return "unknown"
    if eco2 <= fresh_max:
        return "fresh"
    if eco2 <= good_max:
        return "good"
    if eco2 <= stuffy_max:
        return "stuffy"
    return "poor"


def mould_risk_band(dp_c, floor_temp_c, high_gap_c, watch_gap_c):
    """How close the floor sits to the dew point (condensation risk): low/watch/high.
    None on missing dew point or floor reading -> 'unknown' (bathrooms only, see the app)."""
    if dp_c is None or floor_temp_c is None:
        return "unknown"
    gap = floor_temp_c - dp_c
    if gap <= high_gap_c:
        return "high"
    if gap <= watch_gap_c:
        return "watch"
    return "low"


def airing_helps(indoor_dp_c, indoor_rh, outdoor_dp_c, margin_c, rh_min):
    """True only when outdoor air is genuinely drier than indoors AND the room is humid
    enough that airing it would be noticeable."""
    if indoor_dp_c is None or indoor_rh is None or outdoor_dp_c is None:
        return False
    return outdoor_dp_c < indoor_dp_c - margin_c and indoor_rh >= rh_min


def in_time_window(now_time, start_hms, end_hms):
    """Whether a plain datetime.time falls in [start_hms, end_hms) (\"HH:MM\" strings,
    non-wrapping - every configured window here is a single morning span)."""
    try:
        sh, sm = (int(x) for x in str(start_hms).split(":")[:2])
        eh, em = (int(x) for x in str(end_hms).split(":")[:2])
    except (TypeError, ValueError):
        return False
    return dtime(sh, sm) <= now_time < dtime(eh, em)


def render_headline_detail(temp_c, word, air_band, window_open, floor_spread_c,
                            mould_risk, floor_cold_spread_c):
    """Plain-English headline/detail for housemates - see the app docstring's contract."""
    if temp_c is None:
        return "No reading", "Check the room's sensors."
    headline = f"{temp_c:.1f} °C · {word}"
    if mould_risk == "high":
        detail = "Mould risk — dry it out"
    elif air_band in ("stuffy", "poor") and not window_open:
        detail = "Stuffy — open a window"
    elif floor_spread_c is not None and floor_spread_c >= floor_cold_spread_c:
        detail = "Floor still cold"
    else:
        detail = _COMFORT_DETAIL.get(word, "Comfortable")
    return headline, detail


class RoomFeel(hass.Hass):
    # Class-level defaults so bare __new__() test instances are well-defined - same pattern
    # as RoomActive/BedroomComfort in this directory.
    rooms: dict = {}

    def initialize(self):
        a = self.args.get
        self.stale_minutes = float(a("stale_minutes", 45))

        cb = a("comfort_bands", {}) or {}
        self.cold_max_c = float(cb.get("cold_max_c", 18.0))
        self.cool_max_c = float(cb.get("cool_max_c", 20.0))
        self.comfortable_max_c = float(cb.get("comfortable_max_c", 24.0))
        self.warm_max_c = float(cb.get("warm_max_c", 26.0))

        aq = a("air_quality_bands", {}) or {}
        self.iaq_fresh_max = float(aq.get("iaq_fresh_max", 50))
        self.iaq_good_max = float(aq.get("iaq_good_max", 100))
        self.iaq_stuffy_max = float(aq.get("iaq_stuffy_max", 200))
        self.eco2_fresh_max = float(aq.get("eco2_fresh_max", 800))
        self.eco2_good_max = float(aq.get("eco2_good_max", 1200))
        self.eco2_stuffy_max = float(aq.get("eco2_stuffy_max", 2000))

        mb = a("mould_risk_bands", {}) or {}
        self.mould_high_gap_c = float(mb.get("high_gap_c", 1.0))
        self.mould_watch_gap_c = float(mb.get("watch_gap_c", 3.0))

        self.floor_cold_spread_c = float(a("floor_cold_spread_c", 2.0))

        self.outdoor_temp_entity = a("outdoor_temperature_entity", "sensor.gw2000a_outdoor_temperature")
        self.outdoor_rh_entity = a("outdoor_humidity_entity", "sensor.gw2000a_humidity")
        self.airing_dew_margin_c = float(a("airing_dew_margin_c", 1.0))
        self.airing_rh_min = float(a("airing_rh_min", 60))

        supply_air_raw = a("supply_air", {}) or {}
        self.supply_air_source = supply_air_raw.get(
            "source_entity", "sensor.ventilation_thermometer_temperature")
        self.supply_air_publish = supply_air_raw.get(
            "publish_entity", "sensor.bedroom_supply_air_temperature")

        self.rooms = {room: self._parse_room(cfg) for room, cfg in (a("rooms", {}) or {}).items()}
        self._room_state = {room: {"last_cmp": None} for room in self.rooms}

        entity_rooms: dict = {}

        def register(entity, room):
            if entity:
                entity_rooms.setdefault(entity, set()).add(room)

        for room, cfg in self.rooms.items():
            for src in cfg["air_sources"]:
                register(src["entity"], room)
                register(src["rh_entity"], room)
            register(cfg["floor_entity"], room)
            register(cfg["floor_rh_entity"], room)
            for contact in cfg["window_contacts"]:
                register(contact, room)
            if cfg["air_quality"]:
                register(cfg["air_quality"]["aqi_entity"], room)
                register(cfg["air_quality"]["eco2_entity"], room)
            if cfg["sun_hit"]:
                register(cfg["sun_hit"]["radiation_entity"], room)
            register(self.outdoor_temp_entity, room)
            register(self.outdoor_rh_entity, room)

        for entity, room_set in entity_rooms.items():
            self.listen_state(self._on_room_source_change, entity, room_keys=sorted(room_set))

        self.listen_state(self._on_supply_air_change, self.supply_air_source)
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._reconcile, "now+60", 60)

        for room in self.rooms:
            self._evaluate_room(room, force=True)
        self._evaluate_supply_air(force=True)

        self.log(f"RoomFeel: publishing {len(self.rooms)} room(s) + supply-air mirror", level="INFO")

    # ---------- config parsing ----------

    @staticmethod
    def _parse_source(raw):
        return {
            "entity": raw["entity"],
            "attribute": raw.get("attribute"),
            "rh_entity": raw.get("rh_entity"),
            "rh_attribute": raw.get("rh_attribute"),
            "label": raw.get("label", raw["entity"]),
            "sun_flag": bool(raw.get("sun_flag", False)),
            "offset_c": float(raw.get("offset_c", 0.0)),
            "offset_rh": float(raw.get("offset_rh", 0.0)),
        }

    @staticmethod
    def _parse_room(raw):
        raw = raw or {}

        sun_hit_raw = raw.get("sun_hit")
        sun_hit_cfg = None
        if sun_hit_raw:
            sun_hit_cfg = {
                "radiation_entity": sun_hit_raw["radiation_entity"],
                "threshold_w_m2": float(sun_hit_raw.get("threshold_w_m2", 150.0)),
                "start": sun_hit_raw.get("start", "07:00"),
                "end": sun_hit_raw.get("end", "10:00"),
            }

        aq_raw = raw.get("air_quality")
        air_quality_cfg = None
        if aq_raw:
            air_quality_cfg = {
                "aqi_entity": aq_raw.get("aqi_entity"),
                "eco2_entity": aq_raw.get("eco2_entity"),
            }

        return {
            "air_sources": [RoomFeel._parse_source(s) for s in (raw.get("air_sources") or [])],
            "floor_entity": raw.get("floor_entity"),
            "floor_rh_entity": raw.get("floor_rh_entity"),
            "window_contacts": list(raw.get("window_contacts") or []),
            "air_quality": air_quality_cfg,
            "sun_hit": sun_hit_cfg,
            "mould_risk": bool(raw.get("mould_risk", False)),
        }

    # ---------- callbacks ----------

    def _on_room_source_change(self, entity, attribute, old, new, kwargs):
        for room in (kwargs.get("room_keys") if isinstance(kwargs, dict) else None) or []:
            try:
                self._evaluate_room(room)
            except Exception as e:
                self.log(f"ROOMFEEL recompute failed for {room} ({entity}): {e}", level="ERROR")

    def _on_supply_air_change(self, entity, attribute, old, new, kwargs):
        try:
            self._evaluate_supply_air()
        except Exception as e:
            self.log(f"ROOMFEEL supply-air mirror failed: {e}", level="ERROR")

    def _on_plugin_started(self, event_name, data, kwargs):
        self.log("RoomFeel: plugin_started - republishing all rooms", level="INFO")
        for room in self.rooms:
            try:
                self._evaluate_room(room, force=True)
            except Exception as e:
                self.log(f"ROOMFEEL plugin_started recompute failed for {room}: {e}", level="ERROR")
        try:
            self._evaluate_supply_air(force=True)
        except Exception as e:
            self.log(f"ROOMFEEL plugin_started supply-air failed: {e}", level="ERROR")

    def _reconcile(self, kwargs):
        for room in self.rooms:
            try:
                self._evaluate_room(room)
            except Exception as e:
                self.log(f"ROOMFEEL reconcile failed for {room}: {e}", level="ERROR")
        try:
            self._evaluate_supply_air()
        except Exception as e:
            self.log(f"ROOMFEEL reconcile supply-air failed: {e}", level="ERROR")

    # ---------- reads ----------

    def _read(self, entity, attribute=None):
        if not entity:
            return None, "unavailable"
        try:
            raw = self.get_state(entity, attribute=attribute)
        except Exception:
            return None, "unavailable"
        if raw is None or raw == "unavailable":
            return None, "unavailable"
        if raw == "unknown":
            return None, "unknown"
        try:
            return float(raw), None
        except (TypeError, ValueError):
            return None, "non-numeric"

    def _freshest_age_minutes(self, entities, now):
        best = None
        for ent in entities:
            try:
                raw = self.get_state(ent, attribute="last_updated")
                lu = datetime.fromisoformat(str(raw))
                age = (now - lu).total_seconds() / 60.0
            except (TypeError, ValueError):
                age = 0.0
            if best is None or age < best:
                best = age
        return best

    def _sun_hit_active(self, cfg, now):
        if not cfg:
            return False
        if not in_time_window(now.time(), cfg["start"], cfg["end"]):
            return False
        val, reason = self._read(cfg["radiation_entity"])
        return reason is None and val is not None and val >= cfg["threshold_w_m2"]

    # ---------- evaluation ----------

    def _evaluate_room(self, room_key, force=False):
        cfg = self.rooms[room_key]
        now = self.get_now()

        valid = []
        excluded = []
        for src in cfg["air_sources"]:
            t_val, t_reason = self._read(src["entity"], src["attribute"])
            if t_reason is not None:
                excluded.append(f"{src['entity']}: {t_reason}")
                continue
            t_val += src["offset_c"]
            rh_val = None
            if src["rh_entity"]:
                rh_raw, rh_reason = self._read(src["rh_entity"], src["rh_attribute"])
                if rh_reason is None and rh_raw is not None:
                    rh_val = rh_raw + src["offset_rh"]
            valid.append({"src": src, "temp": t_val, "rh": rh_val})

        sun_hit = self._sun_hit_active(cfg["sun_hit"], now)
        sun_excluded = None
        if sun_hit:
            for entry in list(valid):
                if entry["src"]["sun_flag"] and len(valid) > 1:
                    valid.remove(entry)
                    excluded.append(f"{entry['src']['entity']}: sun_hit")
                    sun_excluded = entry["src"]["entity"]

        floor_temp, floor_temp_reason = (
            self._read(cfg["floor_entity"]) if cfg["floor_entity"] else (None, "not_configured"))
        floor_rh, _ = (
            self._read(cfg["floor_rh_entity"]) if cfg["floor_rh_entity"] else (None, "not_configured"))

        used_entities = []
        if valid:
            temp_raw = median([e["temp"] for e in valid])
            rh_values = [e["rh"] for e in valid if e["rh"] is not None]
            rh = median(rh_values)
            for e in valid:
                used_entities.append(e["src"]["entity"])
                if (e["src"]["rh_entity"] and e["rh"] is not None
                        and e["src"]["rh_entity"] != e["src"]["entity"]):
                    used_entities.append(e["src"]["rh_entity"])
            reason = (f"median of {len(valid)} air sources" if len(valid) > 1
                      else f"single air source: {valid[0]['src']['entity']}")
        elif floor_temp is not None:
            temp_raw = floor_temp + 0.5
            rh = floor_rh
            used_entities = [cfg["floor_entity"]]
            if cfg["floor_rh_entity"] and floor_rh is not None:
                used_entities.append(cfg["floor_rh_entity"])
            reason = "floor+0.5°C fallback (no air source available)"
        else:
            temp_raw = None
            rh = None
            reason = "no air or floor source available"
            if cfg["floor_entity"]:
                excluded.append(f"{cfg['floor_entity']}: {floor_temp_reason}")

        if sun_excluded:
            reason += f"; excluded sun-hit source {sun_excluded}"

        temp_c = None if temp_raw is None else round(temp_raw, 1)
        rh = None if rh is None else round(rh, 0)

        floor_spread_c = None
        if temp_c is not None and floor_temp is not None:
            floor_spread_c = round(temp_c - floor_temp, 1)

        dp = dew_point_c(temp_c, rh) if temp_c is not None and rh is not None else None
        dp = None if dp is None else round(dp, 1)
        ah = abs_humidity_g_m3(temp_c, rh) if temp_c is not None and rh is not None else None
        ah = None if ah is None else round(ah, 1)

        if cfg["air_quality"]:
            aqi_val, aqi_reason = self._read(cfg["air_quality"]["aqi_entity"])
            eco2_val, eco2_reason = self._read(cfg["air_quality"]["eco2_entity"])
            if aqi_reason is None and aqi_val is not None:
                air_band = band_from_iaq(aqi_val, self.iaq_fresh_max, self.iaq_good_max, self.iaq_stuffy_max)
            elif eco2_reason is None and eco2_val is not None:
                air_band = band_from_eco2(eco2_val, self.eco2_fresh_max, self.eco2_good_max, self.eco2_stuffy_max)
            else:
                air_band = "unknown"
        else:
            air_band = "unknown"

        window_open = any(self.get_state(c) == "on" for c in cfg["window_contacts"])

        outdoor_t, outdoor_t_reason = self._read(self.outdoor_temp_entity)
        outdoor_rh, outdoor_rh_reason = self._read(self.outdoor_rh_entity)
        outdoor_dp = (dew_point_c(outdoor_t, outdoor_rh)
                      if outdoor_t_reason is None and outdoor_rh_reason is None else None)
        helps = airing_helps(dp, rh, outdoor_dp, self.airing_dew_margin_c, self.airing_rh_min)

        mould = (mould_risk_band(dp, floor_temp, self.mould_high_gap_c, self.mould_watch_gap_c)
                 if cfg["mould_risk"] else "n/a")

        word = comfort_word(temp_c, self.cold_max_c, self.cool_max_c, self.comfortable_max_c, self.warm_max_c)
        stale_age = self._freshest_age_minutes(used_entities, now)
        stale = stale_age is not None and stale_age > self.stale_minutes

        headline, detail = render_headline_detail(
            temp_c, word, air_band, window_open, floor_spread_c, mould, self.floor_cold_spread_c)

        source_entities = []
        for src in cfg["air_sources"]:
            source_entities.append(src["entity"])
            if src["rh_entity"] and src["rh_entity"] != src["entity"]:
                source_entities.append(src["rh_entity"])
        for ent in (cfg["floor_entity"], cfg["floor_rh_entity"]):
            if ent:
                source_entities.append(ent)
        source_entities.extend(cfg["window_contacts"])
        if cfg["air_quality"]:
            for ent in (cfg["air_quality"]["aqi_entity"], cfg["air_quality"]["eco2_entity"]):
                if ent:
                    source_entities.append(ent)
        if cfg["sun_hit"]:
            source_entities.append(cfg["sun_hit"]["radiation_entity"])
        source_entities.append(self.outdoor_temp_entity)
        source_entities.append(self.outdoor_rh_entity)
        seen: set = set()
        source_entities = [e for e in source_entities if e and not (e in seen or seen.add(e))]

        attributes = {
            "temp_c": temp_c,
            "rh": rh,
            "dew_point_c": dp,
            "abs_humidity_g_m3": ah,
            "floor_temp_c": None if floor_temp is None else round(floor_temp, 1),
            "floor_spread_c": floor_spread_c,
            "air_band": air_band,
            "window_open": "true" if window_open else "false",
            "sun_hit": "true" if sun_hit else "false",
            "airing_helps": "true" if helps else "false",
            "mould_risk": mould,
            "sources": used_entities or ["<none>"],
            "excluded": excluded or ["<none>"],
            "stale": "true" if stale else "false",
            "reason": reason,
            "source_entities": source_entities,
            "computed_at": now.isoformat(),
            "headline": headline,
            "detail": detail,
        }

        state = "unavailable" if temp_c is None else temp_c
        self._publish_if_changed(room_key, f"sensor.{room_key}_feel", state, attributes, force)

    def _evaluate_supply_air(self, force=False):
        now = self.get_now()
        val, reason = self._read(self.supply_air_source)
        state = "unavailable" if val is None else round(val, 1)
        attributes = {
            "reason": f"mirror of {self.supply_air_source}",
            "source_entities": [self.supply_air_source],
            "excluded": ["<none>"] if reason is None else [f"{self.supply_air_source}: {reason}"],
            "computed_at": now.isoformat(),
        }
        self._publish_if_changed("__supply_air__", self.supply_air_publish, state, attributes, force)

    def _publish_if_changed(self, state_key, entity_id, state, attributes, force):
        cmp_payload = {"state": state, **{k: v for k, v in attributes.items() if k != "computed_at"}}
        store = self._room_state.setdefault(state_key, {"last_cmp": None})
        changed = store["last_cmp"] != cmp_payload
        if changed or force:
            try:
                self.set_state(entity_id, state=state, replace=True, attributes=dict(attributes))
            except Exception as e:
                self.log(f"ROOMFEEL publish failed for {entity_id}: {e}", level="WARNING")
            store["last_cmp"] = cmp_payload
