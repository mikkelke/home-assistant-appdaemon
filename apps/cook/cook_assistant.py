import asyncio
import json
import math
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass

import cook_fsm as cf
import cook_io as storage
import cook_publish as pub

TICK_S = 10
LIVE_S = 30
TABLET_IDLE_S = 120
SERVICE_TIMEOUT_S = 20
PUSH_ACTIONS = {"COOK_BAG_IN": "bag_in", "COOK_STOP": "bath_stop", "COOK_ICE": "chill",
                "COOK_PULLED": "probe_pulled", "COOK_SILENCE": "silence"}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def command_data(raw):
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


class CookAssistant(hass.Hass):
    def initialize(self):
        here = Path(__file__).resolve().parent
        self.state_file = Path(self.args.get("state_file") or here / "cook_state.json")
        self.ledger_file = Path(self.args.get("ledger_file") or here / "cook_log.json")
        self.media_dir = Path(self.args.get("media_dir") or "/media/cook")
        self.climate_entity = self.args.get("climate_entity", "climate.anova_cooker")
        self.status_entity = self.args.get("status_entity", "sensor.anova_cooker_status")
        self.low_water_entity = self.args.get("low_water_entity", "binary_sensor.anova_cooker_low_water")
        self.stop_entity = self.args.get("stop_entity", "button.anova_cooker_stop_cook")
        self.silence_entity = self.args.get("silence_entity", "button.anova_cooker_silence_alarm")
        prefix = self.args.get("probe_prefix", "sensor.cookperfect_0ca6_")
        self.core_entity = prefix + "core_temperature"
        self.ambient_entity = prefix + "ambient_temperature"
        self.needle_entities = [prefix + f"needle_{index}" for index in range(1, 5)]
        self.tablet_notify = self.args.get("tablet_notify", "notify/mobile_app_oppopad4pro")
        self.tablet_interactive = self.args.get("tablet_interactive", "binary_sensor.oppopad4pro_interactive")
        self.push_link = self.args.get("push_link", "/dashboard-rooftop/home#room=kitchen")
        self._notifier = self.get_app("MobileNotifier")
        self._values = {}
        self._published = {}
        self._curves = {}
        self._ledger_minute = None
        self._last_live = None
        self._device_lock = asyncio.Lock()
        saved = self._read(self.state_file, {})
        saved = saved if isinstance(saved, dict) and saved.get("version") == 1 else {}
        self._tracker = cf.Tracker(saved.get("tracker"))
        self._samples = saved.get("samples", []) if isinstance(saved.get("samples", []), list) else []
        self._samples = [row for row in self._samples if isinstance(row, dict)
                         and number(row.get("at")) is not None][-481:]
        document = self._read(self.ledger_file, {})
        self._records = document.get("cooks", []) if isinstance(document, dict) else []
        self._records = [row for row in self._records if isinstance(row, dict)][-60:]
        entities = [self.climate_entity, self.status_entity, self.low_water_entity, self.core_entity,
                    self.ambient_entity, *self.needle_entities, self.tablet_interactive]
        for entity in entities:
            self._values[entity] = self.get_state(entity, attribute="all") or {}
            self.listen_state(self._on_value, entity, attribute="all")
        self.listen_event(self._on_command, "cook_command")
        self.listen_event(self._on_push_action, "mobile_app_notification_action")
        self.listen_event(self._on_plugin_started, "plugin_started")
        now = self.get_now_ts()
        self._restore_inputs(saved, now)
        self._apply(self._bath_input(now) + self._probe_input(now) + self._tracker.tick(now))
        self._sample(now)
        self._record_active()
        self._checkpoint()
        self._publish(force=True)
        self.run_every(self._on_tick, f"now+{TICK_S}", TICK_S)

    def terminate(self):
        self._checkpoint()
        self._write_curves()

    def _read(self, path, default):
        try:
            return storage.read_json(path, default)
        except (OSError, ValueError) as error:
            self.log(f"Could not read {path.name}: {error!r}", level="WARNING")
            if path.exists():
                backup = path.with_name(f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S%f')}")
                path.rename(backup)
            return default

    def _write(self, path, data):
        try:
            storage.write_json(path, data)
        except (OSError, ValueError, TypeError) as error:
            self.log(f"Could not write {path.name}: {error!r}", level="WARNING")

    def _state(self, entity):
        return (self._values.get(entity) or {}).get("state")

    def _restore_inputs(self, saved, now):
        saved_at = number(saved.get("saved_at"))
        if saved_at is None:
            return
        bath, probe = self._tracker.bath, self._tracker.probe
        climate = self._values.get(self.climate_entity) or {}
        if bath.get("phase") == "cooking" and climate.get("state") != "heat":
            changed = pub.epoch(climate.get("last_changed"))
            since = changed if changed is not None and bath.get("start", now) <= changed <= now else saved_at
            if climate.get("state") == "off" and since < bath["done_at"]:
                bath.update(phase="interrupted", stopped_at=since)
            elif climate.get("state") != "off":
                bath.setdefault("_missing_since", since)
        probe_entity = self.core_entity
        core_index = probe.get("core_index")
        if probe.get("phase") in ("resting", "carve") and isinstance(core_index, int) and 0 <= core_index < 4:
            probe_entity = self.needle_entities[core_index]
        probe_missing = number(self._state(probe_entity)) is None
        if probe.get("phase") not in ("resting", "carve"):
            probe_missing = probe_missing or any(number(self._state(entity)) is None for entity in self.needle_entities)
        if probe.get("phase") != "off" and probe_missing:
            probe.setdefault("_missing_since", saved_at)
            probe.setdefault("stale_since", saved_at)

    def _bath_input(self, now):
        climate = self._values.get(self.climate_entity) or {}
        attrs = climate.get("attributes") or {}
        low_water = self._state(self.low_water_entity) == "on" or self._state(self.status_entity) == "low water"
        return self._tracker.bath_reading(now, climate.get("state"), number(attrs.get("current_temperature")),
                                          number(attrs.get("temperature")), low_water)

    def _probe_input(self, now):
        core = number(self._state(self.core_entity))
        needles = [number(self._state(entity)) for entity in self.needle_entities]
        if self._tracker.probe["phase"] not in ("resting", "carve") and any(value is None for value in needles):
            core = None
        return self._tracker.probe_reading(now, core, number(self._state(self.ambient_entity)), needles)

    def _on_value(self, entity, attribute, old, new, kwargs):
        self._values[entity] = new if isinstance(new, dict) else {"state": new}
        now = self.get_now_ts()
        effects = []
        if entity in (self.climate_entity, self.status_entity, self.low_water_entity):
            effects = self._bath_input(now)
        elif entity in (self.core_entity, self.ambient_entity, *self.needle_entities):
            effects = self._probe_input(now)
        self._apply(effects)
        self._ensure_curves(now)
        self._publish()

    def _on_tick(self, kwargs):
        now = self.get_now_ts()
        self._apply(self._tracker.tick(now))
        if self._last_live is None or now - self._last_live >= LIVE_S:
            self._sample(now)
        minute = int(now // 60)
        if minute != self._ledger_minute:
            self._ledger_minute = minute
            self._record_active()
        self._checkpoint()
        self._publish()

    def _on_command(self, event_name, data, kwargs):
        data = data if isinstance(data, dict) else {}
        payload = command_data(data.get("data"))
        if payload is None:
            self.log("cook_command ignored: invalid data")
            return
        self._apply(self._tracker.command(data.get("op"), payload, self.get_now_ts()))
        self._checkpoint()
        self._publish()

    def _on_push_action(self, event_name, data, kwargs):
        op = PUSH_ACTIONS.get((data or {}).get("action"))
        if op:
            self._on_command("cook_command", {"op": op, "data": {}}, {})

    def _on_plugin_started(self, event_name, data, kwargs):
        self._publish(force=True)

    def _apply(self, effects):
        for effect in effects:
            if effect.get("type") == "closed":
                record = effect["record"]
                self._upsert(record)
                self._write_curves()
                self._curves.pop(record["id"], None)
                self._save_ledger()
        if any(effect.get("type") in ("push", "report", "closed", "service") for effect in effects):
            self._checkpoint()
        for effect in effects:
            kind = effect.get("type")
            if kind == "ignored":
                self.log(f"cook_command ignored: {effect.get('op')!r} in current phase")
            elif kind == "service":
                self.create_task(self._device_command(effect))
            elif kind == "push":
                self.create_task(self._send_push(effect))
                if effect.get("wake"):
                    self._wake_tablet(effect.get("id", "cook"))
            elif kind == "report":
                self.fire_event("house_events_report", icon="mdi:food-steak", text=effect["text"])

    async def _call_service(self, service, **data):
        try:
            envelope = await asyncio.wait_for(self.call_service(service, **data), timeout=SERVICE_TIMEOUT_S)
        except Exception as error:
            self.log(f"{service} failed: {error!r}", level="WARNING")
            return False
        if isinstance(envelope, dict) and envelope.get("success") is False:
            self.log(f"{service} refused: {envelope.get('error')}", level="WARNING")
            return False
        return True

    async def _device_command(self, effect):
        async with self._device_lock:
            await self._device_command_locked(effect)

    async def _device_command_locked(self, effect):
        op = effect.get("op")
        if op == "bath_start":
            if await self._call_service("climate/set_temperature", entity_id=self.climate_entity,
                                        temperature=effect["target"]):
                await self._call_service("climate/set_hvac_mode", entity_id=self.climate_entity, hvac_mode="heat")
        elif op in ("bath_stop", "silence"):
            await self._call_service("button/press", entity_id=self.stop_entity if op == "bath_stop"
                                     else self.silence_entity)

    async def _send_push(self, effect):
        try:
            await self._notifier.notify(title=effect["title"], message=effect.get("message", ""),
                                        target=["mikkel"], category="cook", link=self.push_link,
                                        data={"data": {"tag": f"cook_{effect.get('id', 'alarm')}_{effect['kind']}",
                                                       "actions": effect.get("actions", [])}})
        except Exception as error:
            self.log(f"Cook push failed: {error!r}", level="WARNING")

    def _wake_tablet(self, cook_id):
        full = self._values.get(self.tablet_interactive) or {}
        try:
            changed = datetime.fromisoformat(full.get("last_changed", "").replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return
        if full.get("state") == "off" and self.get_now_ts() - changed >= TABLET_IDLE_S:
            self.create_task(self._wake(cook_id))

    async def _wake(self, cook_id):
        await self._call_service(self.tablet_notify, message="command_screen_on")
        await self._call_service(self.tablet_notify, message="command_webview",
                                 data={"command": f"/dashboard-rooftop/home#cook&takeover={cook_id}"})

    def _sample(self, now):
        at = int(now // LIVE_S) * LIVE_S
        if self._samples and self._samples[-1]["at"] == at:
            self._last_live = now
            return
        bath, probe = self._tracker.bath, self._tracker.probe
        row = self._sample_row(at)
        if self._samples:
            first_missing = max(int(self._samples[-1]["at"]) + LIVE_S, at - 4 * 3600 + LIVE_S)
            for missing in range(first_missing, at, LIVE_S):
                self._samples.append({"at": missing, "bath": None, "core": None, "ambient": None})
        self._samples.append(row)
        self._samples = [sample for sample in self._samples if sample["at"] > at - 4 * 3600]
        self._ensure_curves(now)
        for lane in (bath, probe):
            cook_id = lane.get("id")
            if lane.get("phase") == "off" or not cook_id:
                continue
            curve = self._curves[cook_id]
            rows = curve["samples"]
            if not rows or rows[-1]["at"] < at:
                rows.append(row)
        self._write_curves()
        self._last_live = now
        pulled = probe.get("pulled_at") if probe.get("phase") in ("resting", "carve") else None
        self._set("cook_live", pub.live_entity(self._samples, pulled))

    def _sample_row(self, at):
        bath, probe = self._tracker.bath, self._tracker.probe
        fresh = probe.get("phase") != "off" and probe.get("stale_since") is None
        return {"at": at, "bath": bath.get("temp") if self._state(self.climate_entity) == "heat" else None,
                "core": probe.get("core") if fresh else None, "ambient": probe.get("ambient") if fresh else None}

    def _ensure_curves(self, now):
        for lane in (self._tracker.bath, self._tracker.probe):
            cook_id = lane.get("id")
            if lane.get("phase") == "off" or not cook_id or cook_id in self._curves:
                continue
            curve = self._read(self.media_dir / f"{cook_id}.json", {})
            curve = curve if isinstance(curve, dict) else {}
            curve.update(v=1, id=cook_id, step=LIVE_S)
            if not isinstance(curve.get("samples"), list) or not curve["samples"]:
                curve["samples"] = [self._sample_row(int(now // LIVE_S) * LIVE_S)]
            self._curves[cook_id] = curve
            self._write(self.media_dir / f"{cook_id}.json", curve)

    def _write_curves(self):
        for cook_id, curve in self._curves.items():
            self._write(self.media_dir / f"{cook_id}.json", curve)

    def _upsert(self, record):
        self._records = [row for row in self._records if row.get("id") != record["id"]]
        self._records.append(record)
        self._records = self._records[-60:]

    def _record_active(self):
        for kind, lane in (("bath", self._tracker.bath), ("probe", self._tracker.probe)):
            if lane.get("id") and lane.get("phase") != "off":
                record = {key: value for key, value in lane.items() if not key.startswith("_")}
                record.update(kind=kind)
                self._upsert(record)
        self._save_ledger()

    def _save_ledger(self):
        self._write(self.ledger_file, {"version": 1, "cooks": self._records})

    def _checkpoint(self):
        self._write(self.state_file, {"version": 1, "saved_at": self.get_now_ts(),
                                      "tracker": self._tracker.to_dict(), "samples": self._samples})

    def _set(self, key, payload, force=False):
        if not force and self._published.get(key) == payload:
            return
        state, attributes = payload
        try:
            self.set_state(f"sensor.{key}", state=state, attributes=attributes, replace=True)
        except Exception as error:
            self.log(f"Publish {key} failed: {error!r}", level="WARNING")
            return
        self._published[key] = payload

    def _publish(self, force=False):
        self._set("cook_state", pub.state_entity(self._tracker.bath, self._tracker.probe, self.get_now_ts()), force)
        self._set("cook_log", pub.log_entity(self._records), force)
        if force:
            probe = self._tracker.probe
            pulled = probe.get("pulled_at") if probe.get("phase") in ("resting", "carve") else None
            self._set("cook_live", pub.live_entity(self._samples, pulled), force=True)
