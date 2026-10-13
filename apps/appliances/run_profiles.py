"""RunProfiles - publishes sensor.appliance_run_profiles: per appliance and programme, the
minute-level energy profile measured from real past cycles, for the dashboard's start-time
cost planning. The rules live in run_profile_model.py; this app only reads, schedules and
publishes.

Reads, never writes: the monitors' <appliance>_feedback.json files and the recorder history
of each appliance's state and power entity. Its only file is the extraction cache
(cache_file, ends in _state.json so it is never tracked or deployed).

Entity contract (schema 1):
  state       UTC ISO time of the last content change
  attributes  schema 1, unit "dWh/min",
              appliances: {washer|dishwasher|dryer: {
                  records   feedback records (absent = 0)
                  profiled  energy-validated cycles (absent = 0)
                  programmes: [{key, label, n, kwh, run_min, done_min (absent = unknown),
                                label_source confirmed|inferred|mixed,
                                p "comma-separated dWh per minute from power onset"}]}}
  "typical" comes first, then programmes by use over the last 90 days.

Scan: every scan_minutes, and again after 60 s while a backlog remains. Each run reads the
feedback files, extracts at most records_per_run new or changed records (newest first)
within run_budget_s, rebuilds and publishes only on change. A history read that fails is
retried on a later run without blocking older records; a feedback file that cannot be
read keeps that appliance's last published block and its cache entries. Runs never
overlap.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import run_profile_model as model

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_VERSION = 1
RETRY_AFTER_S = 900
BACKLOG_DELAY_S = 60


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class RunProfiles(hass.Hass):
    def initialize(self):
        self.entity = self.args.get("entity", "sensor.appliance_run_profiles")
        self.cache_file = self.args.get("cache_file") or os.path.join(HERE, "run_profiles_cache_state.json")
        self.records_per_run = int(self.args.get("records_per_run", 10))
        self.run_budget_s = float(self.args.get("run_budget_s", 60))
        self.max_payload_bytes = int(self.args.get("max_payload_bytes", model.MAX_PAYLOAD_BYTES))
        self.appliances = {name: self._parse_appliance(name, cfg)
                           for name, cfg in (self.args.get("appliances") or {}).items()}
        self._labels = {name: self._load_labels(cfg["programmes_file"]) for name, cfg in self.appliances.items()}
        self._scanning = False
        self._retry_at = {}
        self._cache = self._load_cache()
        published = self._cache.get("published")
        if published:
            self._publish(published)
        self.run_in(self._scan, 10)
        scan_s = int(float(self.args.get("scan_minutes", 15)) * 60)
        self.run_every(self._scan, f"now+{scan_s}", scan_s)
        self.run_every(self._ensure_published, "now+3600", 3600)
        self.log(f"RunProfiles: {', '.join(self.appliances)} -> {self.entity}", level="INFO")

    @staticmethod
    def _parse_appliance(name, cfg):
        cfg = dict(cfg or {})
        return {
            "feedback_file": cfg.get("feedback_file") or os.path.join(HERE, f"{name}_feedback.json"),
            "programmes_file": cfg.get("programmes_file") or os.path.join(HERE, f"{name}_programmes.yaml"),
            "state_entity": cfg["state_entity"],
            "power_entity": cfg["power_entity"],
            "onset_w": float(cfg["onset_w"]),
        }

    @staticmethod
    def _source(cfg):
        return {k: cfg[k] for k in ("state_entity", "power_entity", "onset_w")}

    # -- files ---------------------------------------------------------------------------

    def _load_labels(self, path):
        try:
            import yaml

            with open(path, "r") as f:
                data = yaml.safe_load(f) or {}
            return {k: v["label"] for k, v in (data.get("programmes") or {}).items()
                    if isinstance(v, dict) and v.get("label")}
        except Exception as e:
            self.log(f"RunProfiles: no programme labels from {path}: {e}", level="WARNING")
            return {}

    def _load_cache(self):
        try:
            with open(self.cache_file, "r") as f:
                data = json.load(f)
            if data.get("version") == CACHE_VERSION and isinstance(data.get("entries"), dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log(f"RunProfiles: cache unreadable, starting empty: {e}", level="WARNING")
        return {"version": CACHE_VERSION, "entries": {}}

    def _save_cache(self):
        tmp = f"{self.cache_file}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._cache, f, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp, self.cache_file)
        except Exception as e:
            self.log(f"RunProfiles: cache write failed: {e}", level="WARNING")

    def _read_feedback(self, path):
        """The file's cycle records, or None when it cannot be read or parsed."""
        try:
            with open(path, "r") as f:
                cycles = json.load(f).get("cycles")
            return cycles if isinstance(cycles, list) else None
        except Exception as e:
            self.log(f"RunProfiles: feedback {path} unreadable this run: {e}", level="WARNING")
            return None

    # -- history -------------------------------------------------------------------------

    def _history(self, entity, start, end, **kwargs):
        """History rows for one entity, or None when the read failed (retryable)."""
        try:
            result = self.get_history(
                entity_id=entity,
                start_time=datetime.fromtimestamp(start, timezone.utc),
                end_time=datetime.fromtimestamp(end, timezone.utc),
                **kwargs,
            )
        except Exception as e:
            self.log(f"RunProfiles: history {entity} failed: {e}", level="WARNING")
            return None
        if result is None:
            return None
        return model.flatten_history(result)

    def _extract(self, name, rec):
        """New cache entry for one record, or None when a history read failed."""
        cfg = self.appliances[name]
        ts = model.record_ts(rec)
        start, end = model.state_window(rec)
        rows = self._history(cfg["state_entity"], start, end, significant_changes_only="0")
        if rows is None:
            return None
        cst = model.find_cycle_start(rows, ts, model.expected_start(name, rec))
        if cst is None:
            entry = {"status": "no_start"}
        else:
            p_start, p_end = model.power_window(cst, ts)
            rows = self._history(cfg["power_entity"], p_start, p_end, minimal_response=True, no_attributes=True)
            if rows is None:
                return None
            entry = model.extract_power(rows, cst, model.cycle_end(name, rec, cst), cfg["onset_w"])
        entry["fp"] = model.extraction_fingerprint(rec, self._source(cfg))
        return entry

    # -- scan ----------------------------------------------------------------------------

    def _pending(self, records, now):
        """(ts, appliance, record) needing extraction, newest first, skipping deferred retries."""
        todo = []
        entries = self._cache["entries"]
        for name, recs in records.items():
            if recs is None:
                continue
            source = self._source(self.appliances[name])
            for rec in recs:
                if model.record_eligibility(rec) is not None:
                    continue
                if model.current_entry(name, rec, entries, source) is not None:
                    continue
                if self._retry_at.get(model.cache_key(name, rec), 0) > now:
                    continue
                todo.append((model.record_ts(rec), name, rec))
        todo.sort(key=lambda item: item[0], reverse=True)
        return todo

    def _prune(self, records):
        entries = self._cache["entries"]
        changed = False
        for name, recs in records.items():
            if recs is None:
                continue
            live = {model.cache_key(name, r) for r in recs}
            for key in [k for k in entries if k.startswith(f"{name}|") and k not in live]:
                del entries[key]
                changed = True
        return changed

    def _scan(self, kwargs=None):
        if self._scanning:
            return
        self._scanning = True
        try:
            self._scan_once()
        except Exception as e:
            self.log(f"RunProfiles: scan failed: {e}", level="ERROR")
        finally:
            self._scanning = False

    def _scan_once(self):
        started = time.monotonic()
        records = {name: self._read_feedback(cfg["feedback_file"]) for name, cfg in self.appliances.items()}
        dirty = self._prune(records)
        pending = self._pending(records, time.time())
        done = 0
        for _, name, rec in pending:
            if done >= self.records_per_run or time.monotonic() - started >= self.run_budget_s:
                break
            done += 1
            key = model.cache_key(name, rec)
            entry = self._extract(name, rec)
            if entry is None:
                self._retry_at[key] = time.time() + RETRY_AFTER_S
                continue
            self._retry_at.pop(key, None)
            self._cache["entries"][key] = entry
            dirty = True
        attributes = self._build(records)
        if attributes is not None and attributes != (self._cache.get("published") or {}).get("attributes"):
            self._cache["published"] = {"state": _iso(time.time()), "attributes": attributes}
            self._publish(self._cache["published"])
            dirty = True
        if dirty:
            self._save_cache()
        if len(pending) > done:
            self.run_in(self._scan, BACKLOG_DELAY_S)

    def _build(self, records):
        """Attributes from the cache; an appliance whose records could not be read (or whose
        build failed) keeps its last published block."""
        previous = ((self._cache.get("published") or {}).get("attributes") or {}).get("appliances") or {}
        now = time.time()
        blocks = {}
        for name, recs in records.items():
            if recs is None:
                if name in previous:
                    blocks[name] = self._restore_block(previous[name])
                continue
            try:
                blocks[name] = model.build_appliance(name, recs, self._cache["entries"],
                                                     self._source(self.appliances[name]), now, self._labels.get(name))
            except Exception as e:
                self.log(f"RunProfiles: build failed for {name}: {e}", level="ERROR")
                if name in previous:
                    blocks[name] = self._restore_block(previous[name])
        if not blocks:
            return None
        attributes = model.build_attributes(blocks, self.max_payload_bytes)
        if not model.transport_safe(attributes):
            self.log("RunProfiles: built attributes would not survive publishing; keeping last good", level="ERROR")
            return None
        return attributes

    @staticmethod
    def _restore_block(block):
        restored = dict(block)
        restored["programmes"] = [{**p, "_use": len(block.get("programmes", [])) - i}
                                  for i, p in enumerate(block.get("programmes", []))]
        return restored

    # -- publish -------------------------------------------------------------------------

    def _publish(self, published):
        try:
            self.set_state(self.entity, state=published["state"], attributes=dict(published["attributes"]),
                           replace=True, check_existence=False)
        except Exception as e:
            self.log(f"RunProfiles: publish failed: {e}", level="WARNING")

    def _ensure_published(self, kwargs=None):
        published = self._cache.get("published")
        if not published:
            return
        try:
            missing = self.get_state(self.entity) is None
        except Exception:
            missing = True
        if missing:
            self.log("RunProfiles: entity missing, republishing", level="INFO")
            self._publish(published)
