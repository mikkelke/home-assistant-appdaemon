"""HouseUpcoming - what is coming up in the house that only the server can know, for the dashboard's
Now timeline: maintenance (the Rober2 parts, the washer drum clean, batteries by their replace-by
day and offline devices; admin only), Kristine's train, and rain while a terrace door is open
(exploratory).

Publishes sensor.house_upcoming (shape in upcoming_core.py) plus one maintenance push a day, built
from the same conditions the rows show. The lease in `expires` is renewed every TICK_S even when
nothing changed, so the dashboard drops the rows within upcoming_core.LEASE once this app stops.

State acquisition: one snapshot of every source at initialize, then listen_state(attribute="all"),
so attribute-only changes (departures, last_drum_clean) count. A value from a callback is never
replaced by a later read, because AppDaemon 4.5.13's state store can lag behind HA; the daily
history job refreshes battery readings from the recorder when it holds a newer one. The last drum
clean seen is kept while washer_state is missing or lacks it. HA restarts re-run initialize, and
plugin_started republishes.

The daily history job also turns each device's recorder history into battery facts and a
replace-by day, with the median lifetime of every device of the same model as the last resort.

Latches, push keys and battery facts persist in house_upcoming_state.json next to this module; rain
forecast fixtures go to house_upcoming_rain_fixtures.jsonl.
"""

import asyncio
import hashlib
import json
import os
import time as monotonic_time
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import climate_model as cm  # apps/climate - AppDaemon puts every app dir on sys.path
import upcoming_battery_age as age
import upcoming_core as core
import upcoming_maint as maint
import upcoming_rain as rain
import upcoming_train as train

TICK_S = 300
HISTORY_DELAY_S = 45
STATE_WRITE_MIN_S = 60
FORECAST_TIMEOUT_S = 12
FETCH_STUCK = timedelta(minutes=5)
STATE_VERSION = 1


def _clock(value, default):
    return time.fromisoformat(str(value or default))


class HouseUpcoming(hass.Hass):
    def initialize(self):
        a = self.args
        here = Path(__file__).resolve().parent
        self.entity = a.get("entity", "sensor.house_upcoming")
        self.state_file = Path(a.get("state_file") or here / "house_upcoming_state.json")
        self.fixture_file = Path(a.get("fixture_file") or here / "house_upcoming_rain_fixtures.jsonl")
        self.push_target = a.get("push_target", "mikkel")
        self.push_link = a.get("push_link", "/dashboard-rooftop/home#now")
        start = datetime.fromisoformat(str(a.get("history_start", "2025-01-26")))
        self.history_start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)

        rober2 = a.get("rober2") or {}
        self.rober2_entities = {
            part: f"{rober2.get('prefix', 'sensor.rober2_')}{part}{rober2.get('suffix', '_time_left')}"
            for part, _, _ in maint.ROBER2_PARTS
        }
        self.rober2_area = rober2.get("area", "kitchen")
        washer = a.get("washer") or {}
        self.washer_entity = washer.get("entity", "sensor.washer_state")
        self.washer_area = washer.get("area", "guest_bathroom")
        tr = a.get("train") or {}
        self.train_route = tr.get("route", "sensor.transit_kbh_ic_odense")
        self.train_stog = tr.get("stog_status", "input_select.transit_carlsberg_stog_kbh_status")
        self.train_person = tr.get("person", "person.kristine")
        self.train_users = tuple(tr.get("users") or ["Kristine"])
        rn = a.get("rain") or {}
        self.weather_entity = rn.get("weather", "weather.forecast_home")
        self.rain_alert = rn.get("alert", "binary_sensor.weather_opening_alert_rooftop_rain")
        self.rain_doors = dict(rn.get("doors") or {})
        self.rain_both_area = rn.get("both_area", "rooftop")
        self.inventory = maint.parse_inventory(a.get("batteries"))

        self._notifier = self.get_app("MobileNotifier")

        saved = self._load_state()
        self._rober2 = dict(saved.get("rober2") or {})
        self._batteries = dict(saved.get("batteries") or {})
        self._pushed = dict(saved.get("pushed") or {})
        self._facts = dict(saved.get("facts") or {})
        self._content = dict(saved.get("content") or {})
        self._drum_seen = saved.get("drum") if isinstance(saved.get("drum"), str) else None
        self._state_text = None
        self._state_written_at = None
        self._cache = {}
        self._offline_at = {}
        self._conditions = []
        self._forecast = None
        self._last_fetch = None
        self._fetching = False
        self._handoff = {}

        for entity in self._sources():
            self._cache[entity] = self._read(entity)
            self.listen_state(self._on_source, entity, attribute="all")
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._on_tick, f"now+{TICK_S}", TICK_S)
        self.run_daily(self._on_push_time, _clock(a.get("push_time"), "09:00:00"))
        self.run_daily(self._history_job, _clock(a.get("history_time"), "03:40:00"))
        self.run_in(self._history_job, HISTORY_DELAY_S)

        self._check_inventory()
        self._evaluate(publish=True)
        self.log(f"HouseUpcoming initialized - {len(self.inventory['devices'])} battery devices, "
                 f"{len(self._sources())} sources, {self._row_count} rows")

    def terminate(self):
        self._save_state(force=True)

    # -- sources --------------------------------------------------------------

    def _sources(self):
        sources = list(self.rober2_entities.values())
        sources += [self.washer_entity, self.train_route, self.train_stog, self.train_person, self.rain_alert]
        sources += list(self.rain_doors) + list(self.inventory["devices"])
        return list(dict.fromkeys(sources))

    def _read(self, entity):
        try:
            full = self.get_state(entity, attribute="all")
        except Exception as e:
            self.log(f"Read of {entity} failed: {e!r}", level="WARNING")
            return None
        return full if isinstance(full, dict) else None

    def _state_of(self, entity):
        return (self._cache.get(entity) or {}).get("state")

    def _check_inventory(self):
        try:
            sensors = self.get_state("sensor") or {}
        except Exception as e:
            self.log(f"Battery inventory check failed: {e!r}", level="WARNING")
            return
        batteries = [entity for entity, full in sensors.items()
                     if ((full or {}).get("attributes") or {}).get("device_class") == "battery"]
        unlisted = maint.unlisted_batteries(batteries, self.inventory)
        missing = sorted(entity for entity in self.inventory["devices"] if entity not in sensors)
        if unlisted:
            self.log(f"Battery sensors neither listed nor excluded: {', '.join(unlisted)}", level="WARNING")
        if missing:
            self.log(f"Listed battery sensors HA does not have: {', '.join(missing)}", level="WARNING")
        if not unlisted and not missing:
            self.log(f"Battery inventory complete: {len(self.inventory['devices'])} listed, "
                     f"{len(self.inventory['excluded'])} excluded")

    # -- callbacks ------------------------------------------------------------

    def _on_source(self, entity, attribute, old, new, kwargs):
        self._cache[entity] = new if isinstance(new, dict) else None
        if entity in self.inventory["devices"] and isinstance(new, dict):
            was = maint.reading(old.get("state")) if isinstance(old, dict) else None
            if was is not None and maint.reading(new.get("state")) is None:
                self._offline_at[entity] = maint.parse_iso(new.get("last_changed")) or self.get_now()
        self._evaluate()

    def _on_plugin_started(self, event_name, data, kwargs):
        self._evaluate(publish=True)

    def _on_tick(self, kwargs):
        for entity, cached in list(self._cache.items()):
            if cached is None:
                self._cache[entity] = self._read(entity)
        self._evaluate(publish=True)
        self._save_state(force=True)

    # -- evaluation -----------------------------------------------------------

    def _evaluate(self, publish=False):
        now = self.get_now()
        rows, conditions = [], []

        for part, entity in self.rober2_entities.items():
            self._rober2[part] = maint.step_rober2(self._rober2.get(part), maint.reading(self._state_of(entity)), now)
        found = maint.rober2_rows(self._rober2, self.rober2_area)
        drum = ((self._cache.get(self.washer_entity) or {}).get("attributes") or {}).get("last_drum_clean")
        if isinstance(drum, str):
            self._drum_seen = drum
        found.append(maint.drum_row(self._drum_seen, now, self.washer_area))
        for entity, device in self.inventory["devices"].items():
            cached = self._cache.get(entity)
            if cached is None:
                continue
            model = self.inventory["models"][device["model"]]
            value = maint.reading(cached.get("state"))
            self._batteries[entity] = maint.step_battery(self._batteries.get(entity), model, value, now,
                                                         self._offline_at.pop(entity, None))
            found.append(maint.battery_row(entity, device, model, self._batteries[entity], value,
                                           self._facts.get(entity), now))
        for item in found:
            if item:
                rows.append(item[0])
                conditions.append((item[0]["title"], item[1]))

        rows.append(train.train_row(self._cache.get(self.train_route), self._state_of(self.train_stog),
                                    self._state_of(self.train_person), now, self.train_users))
        rows.append(self._rain_row(now))

        rows = core.finalize_rows([row for row in rows if row])
        self._conditions = conditions
        self._row_count = len(rows)
        digest = hashlib.sha1(core.canonical(rows).encode()).hexdigest()
        changed = digest != self._content.get("rows")
        if changed:
            self._content = {"rows": digest, "changed": core.utc_iso(now)}
        if changed or publish:
            self._publish(rows, now)
        self._save_state()

    def _rain_row(self, now):
        doors = {}
        for entity in self.rain_doors:
            cached = self._cache.get(entity) or {}
            doors[entity] = (cached.get("state") == "on", maint.parse_iso(cached.get("last_changed")))
        open_long = rain.doors_open_long(doors, now)
        in_flight = self._fetching and now - self._last_fetch < FETCH_STUCK
        if not in_flight and rain.fetch_due(open_long, doors, self._last_fetch, now):
            self._fetching = True
            self._last_fetch = now
            self.log(f"RAIN fetch: {', '.join(open_long)} open")
            self.create_task(self._fetch_forecast(open_long))
        hours, fetched = self._forecast or ([], None)
        return rain.rain_row(hours, fetched, open_long, self.rain_doors, self._state_of(self.rain_alert) == "on",
                             now, self.rain_both_area)

    def _publish(self, rows, now):
        try:
            self.set_state(
                self.entity,
                state=self._content["changed"],
                attributes={
                    "friendly_name": "House upcoming",
                    "icon": "mdi:timeline-clock-outline",
                    "expires": core.lease_until(now),
                    "rows": rows,
                },
                replace=True,
            )
        except Exception as e:
            self.log(f"Publish failed: {e!r}", level="WARNING")

    # -- rain forecast --------------------------------------------------------

    async def _fetch_forecast(self, open_long):
        entries = None
        try:
            resp = await asyncio.wait_for(
                self.call_service("weather/get_forecasts", entity_id=self.weather_entity, type="hourly",
                                  return_response=True),
                timeout=FORECAST_TIMEOUT_S,
            )
            entries = cm.parse_forecast_envelope(resp, self.weather_entity)
        except Exception as e:
            self.log(f"RAIN fetch failed: {e!r}", level="WARNING")
        finally:
            self._handoff["forecast"] = (self._last_fetch, entries, open_long)
            await self.run_in(self._apply_forecast, 0)

    def _apply_forecast(self, kwargs):
        fetched, entries, open_long = self._handoff.pop("forecast", (None, None, []))
        self._fetching = False
        if fetched is not None and entries is not None:
            hours = rain.forecast_hours(entries)
            self._forecast = (hours, fetched)
            row = rain.rain_row(hours, fetched, open_long, self.rain_doors, False, fetched, self.rain_both_area)
            self._append_fixture(rain.fixture(fetched, hours, open_long, row))
            self.log(f"RAIN forecast: {len(hours)} hours, row at {row.get('at') if row else 'none'}")
        self._evaluate()

    def _append_fixture(self, record):
        try:
            with open(self.fixture_file, "a") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception as e:
            self.log(f"Rain fixture write failed: {e!r}", level="WARNING")

    # -- battery history ------------------------------------------------------

    async def _history_job(self, kwargs):
        now = await self.get_now()
        series, latest = {}, {}
        for entity in self.inventory["devices"]:
            try:
                rows = await self.get_history(entity_id=entity, start_time=self.history_start, end_time=now,
                                              minimal_response=True, no_attributes=True)
            except Exception as e:
                self.log(f"History of {entity} failed: {e!r}", level="WARNING")
                continue
            flat = [row for group in rows or [] for row in (group if isinstance(group, list) else [group])]
            series[entity] = age.series_from_history(flat)
            if flat and isinstance(flat[-1], dict):
                latest[entity] = flat[-1]
        self._handoff["history"] = (now, series, latest)
        await self.run_in(self._apply_history, 0)

    def _apply_history(self, kwargs):
        fetched, series_by_entity, latest = self._handoff.pop("history", (None, {}, {}))
        if fetched is None:
            return
        for entity, series in series_by_entity.items():
            device = self.inventory["devices"][entity]
            model = self.inventory["models"][device["model"]]
            days = age.daily_stats(series, fetched)
            facts = age.battery_facts(days, fetched, trend=model["trend"], history_from=device["history_from"],
                                      empty=model["empty"])
            self._facts[entity] = facts
            onset, had_reading = age.outage_onset(series)
            in_since = age.as_date(facts.get("in_since"))
            self._batteries[entity] = maint.bootstrap_battery(
                self._batteries.get(entity),
                model,
                onset=onset,
                outage_from=None if had_reading or not series else self.history_start,
                low_dates=age.low_dates(days, model["low"], fetched) if model["low"] is not None else (),
                peak=age.peak_since(days, in_since) if model["drop"] is not None else None,
            )
            self._refresh_from_recorder(entity, latest.get(entity))
        self._estimate_replace_by(maint.house_date(fetched))
        self.log(f"Battery history read for {len(series_by_entity)} devices")
        self._evaluate()

    def _estimate_replace_by(self, today):
        lifetimes = {}
        for entity, device in self.inventory["devices"].items():
            lifetimes.setdefault(device["model"], []).extend((self._facts.get(entity) or {}).get("lifetimes") or [])
        for entity, device in self.inventory["devices"].items():
            facts = {k: v for k, v in (self._facts.get(entity) or {}).items() if k not in ("replace_by", "basis")}
            due = age.replace_by(facts, lifetimes[device["model"]], today, device["history_from"])
            if due:
                facts.update(replace_by=due[0].isoformat(), basis=due[1])
            if facts:
                self._facts[entity] = facts

    def _refresh_from_recorder(self, entity, row):
        """The recorder's newest state wins over a cached one that is older, which is how a reading
        AppDaemon's store missed still arrives."""
        if not isinstance(row, dict) or "state" not in row:
            return
        when = row.get("last_changed")
        when = when if isinstance(when, datetime) else maint.parse_iso(when)
        cached = self._cache.get(entity)
        cached_at = maint.parse_iso((cached or {}).get("last_changed"))
        if when is None or cached is None or (cached_at is not None and cached_at >= when):
            return
        if cached.get("state") != row["state"]:
            self.log(f"{entity}: recorder has {row['state']!r} since {core.utc_iso(when)}, "
                     f"the store had {cached.get('state')!r}")
        self._cache[entity] = {**cached, "state": row["state"], "last_changed": core.utc_iso(when)}

    # -- daily push -----------------------------------------------------------

    def _on_push_time(self, kwargs):
        now = self.get_now()
        self._pushed = maint.prune_pushed(self._pushed, now)
        found = maint.digest(self._conditions, self._pushed)
        if found and "push" not in self._handoff:
            self._handoff["push"] = None
            self.create_task(self._send_digest(found))

    async def _send_digest(self, found):
        delivered = 0
        try:
            delivered = await self._notifier.notify(title=found["title"], message=found["message"],
                                                    target=[self.push_target], link=self.push_link)
        except Exception as e:
            self.log(f"Maintenance push failed: {e!r}", level="WARNING")
        self._handoff["push"] = (found, delivered or 0)
        await self.run_in(self._apply_push, 0)

    def _apply_push(self, kwargs):
        found, delivered = self._handoff.pop("push", None) or (None, 0)
        if not found:
            return
        if not delivered:
            self.log("Maintenance push not delivered; tomorrow's digest retries it", level="WARNING")
            return
        stamp = core.utc_iso(self.get_now())
        for key in found["keys"]:
            self._pushed[key] = stamp
        self._save_state(force=True)
        self.log(f"Maintenance push sent: {found['message']}")

    # -- persistence ----------------------------------------------------------

    def _load_state(self):
        try:
            data = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.log(f"Could not read {self.state_file.name}: {e!r}", level="WARNING")
            return {}
        return data if isinstance(data, dict) and data.get("version") == STATE_VERSION else {}

    def _save_state(self, force=False):
        data = {"version": STATE_VERSION, "rober2": self._rober2, "batteries": self._batteries,
                "pushed": self._pushed, "facts": self._facts, "content": self._content, "drum": self._drum_seen}
        text = json.dumps(data, sort_keys=True)
        if text == self._state_text:
            return
        now = monotonic_time.monotonic()
        if not force and self._state_written_at is not None and now - self._state_written_at < STATE_WRITE_MIN_S:
            return
        try:
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(text)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"State file write failed: {e!r}", level="WARNING")
            return
        self._state_text = text
        self._state_written_at = now
