"""HouseScenarios - sensor.house_scenarios: what was going on in the house, built from sensor.house_events rows
(plus the rows of feed_backfill_entity) and the house's own sessions by scenarios_core (door visits, nights, machine runs, cooking).

Observer only. Each change of the feed or of a signal entity rebuilds the scenarios a few seconds later from
the rows and signal readings of the last KEEP_S (rows kept in house_scenarios_state.json beside this module,
readings read back from Home Assistant's history at each start), and publishes the ones still going on or ended within PUBLISH_S:
    attributes.scenarios: oldest first, scenarios_core's dicts
    state: how many are going on
The recorder keeps every published state, so older scenarios are read back from its history, and the days
before this app ran come from house_scenarios_backfill.json (scripts/scenario_replay.py), published whole to
backfill_entity.
"""

import json
from datetime import timedelta
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import scenarios_core as core

KEEP_S = 3 * 86400
PUBLISH_S = 2 * 86400
PUBLISH_MAX = 30
REBUILD_DELAY_S = 3
SEED_POWER_S = 12 * 3600


def append_point(points, t, value):
    """Adds a reading when it differs from the last one. True when added."""
    if points and points[-1][1] == value:
        return False
    points.append((t, value))
    return True


def power_point(points, t, value, threshold):
    """Adds a power reading only where it crosses the threshold, which is all a power session needs."""
    try:
        watts = float(value)
    except (TypeError, ValueError):
        return False
    above = watts > threshold
    if points:
        try:
            if (float(points[-1][1]) > threshold) == above:
                return False
        except (TypeError, ValueError):
            pass
    points.append((t, watts))
    return True


def prune(points, oldest):
    """The readings from `oldest` on, plus the last one before it: the state at `oldest`."""
    index = 0
    while index + 1 < len(points) and points[index + 1][0] <= oldest:
        index += 1
    return points[index:]


class HouseScenarios(hass.Hass):
    def initialize(self):
        self.feed_entity = self.args.get("feed_entity", "sensor.house_events")
        self.feed_backfill_entity = self.args.get("feed_backfill_entity")
        self.entity = self.args.get("scenarios_entity", "sensor.house_scenarios")
        self.night_entity = self.args["night_entity"]
        self.machine_entities = dict(self.args.get("machines") or {})
        self.power_entities = dict(self.args.get("power") or {})
        self.area_entities = dict(self.args.get("areas") or {})
        self.state_file = Path(__file__).with_name("house_scenarios_state.json")
        self.rows = {}
        self.signals = {"night": [], "machines": {k: [] for k in self.machine_entities},
                        "power": {k: [] for k in self.power_entities}, "areas": {k: [] for k in self.area_entities}}
        self.published = None
        self.rebuild_handle = None
        self._load()
        self._seed()
        self._take_rows(self.get_state(self.feed_entity, attribute="events"))
        self.listen_state(self._on_feed, self.feed_entity, attribute="events")
        if self.feed_backfill_entity:
            self._take_rows(self.get_state(self.feed_backfill_entity, attribute="events"))
            self.listen_state(self._on_feed, self.feed_backfill_entity, attribute="events")
        self.listen_state(self._on_signal, self.night_entity, kind="night")
        for key, entity in self.machine_entities.items():
            self.listen_state(self._on_signal, entity, kind="machines", key=key)
        for key, entity in self.power_entities.items():
            self.listen_state(self._on_signal, entity, kind="power", key=key)
        for key, entity in self.area_entities.items():
            self.listen_state(self._on_signal, entity, kind="areas", key=key)
        self.run_every(self._rebuild, "now+900", 900)
        self._publish_backfill()
        self._rebuild({})

    # -- inputs ------------------------------------------------------------

    def _now(self):
        return self.get_now().timestamp()

    def _points(self, kind, key=None):
        return self.signals[kind] if key is None else self.signals[kind][key]

    def _load(self):
        """The rows kept from earlier feeds; the signals are read back from Home Assistant's history instead."""
        try:
            saved = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        self._take_rows(saved.get("rows"))

    def _seed(self):
        """Each signal's readings over its window, from Home Assistant's history."""
        now = self.get_now()
        seeds = [("night", None, self.night_entity, KEEP_S)]
        seeds += [("machines", k, e, KEEP_S) for k, e in self.machine_entities.items()]
        seeds += [("power", k, e, SEED_POWER_S) for k, e in self.power_entities.items()]
        seeds += [("areas", k, e, KEEP_S) for k, e in self.area_entities.items()]
        for kind, key, entity, span in seeds:
            # With an entity_id AppDaemon ignores end_time, and Home Assistant reads one day from a start_time,
            # so the window is read a day at a time.
            for day in range(-(-span // 86400)):
                start = now - timedelta(seconds=span - day * 86400)
                try:
                    history = self.get_history(entity_id=entity, start_time=start)
                except Exception as e:
                    self.log(f"History seed for {entity} failed: {e}", level="WARNING")
                    break
                for state in (history[0] if history else []):
                    t = core.epoch(state.get("last_changed"))
                    if t is not None and t <= now.timestamp():
                        self._add(kind, key, t, state.get("state"))

    def _add(self, kind, key, t, value):
        if value in (None, "unknown", "unavailable"):
            if kind != "areas":
                return False
            value = None
        points = self._points(kind, key)
        if points and t < points[-1][0]:
            return False
        if kind == "power":
            return power_point(points, t, value, core.POWER_THRESHOLDS.get(key, 0.0))
        return append_point(points, t, value)

    def _take_rows(self, events):
        for row in events if isinstance(events, list) else []:
            if isinstance(row, dict) and isinstance(row.get("ts"), str):
                self.rows[f"{row['ts']}|{row.get('text')}"] = row

    def _on_feed(self, entity, attribute, old, new, kwargs):
        self._take_rows(new)
        self._schedule()

    def _on_signal(self, entity, attribute, old, new, kwargs):
        if self._add(kwargs["kind"], kwargs.get("key"), self._now(), new):
            self._schedule()

    def _schedule(self):
        if self.rebuild_handle is None:
            self.rebuild_handle = self.run_in(self._rebuild, REBUILD_DELAY_S)

    # -- output ------------------------------------------------------------

    def _rebuild(self, kwargs):
        self.rebuild_handle = None
        now = self._now()
        oldest = now - KEEP_S
        self.rows = {key: row for key, row in self.rows.items() if (core.epoch(row["ts"]) or 0) >= oldest}
        self.signals["night"] = prune(self.signals["night"], oldest)
        for kind in ("machines", "power", "areas"):
            for key in self.signals[kind]:
                self.signals[kind][key] = prune(self.signals[kind][key], oldest)
        try:
            scenarios = core.build_scenarios(list(self.rows.values()), self.signals, now)
        except Exception as e:
            self.log(f"Scenario build failed: {e}", level="ERROR")
            return
        shown = core.recent(scenarios, now, PUBLISH_S, PUBLISH_MAX)
        self._save()
        if shown == self.published:
            return
        self.published = shown
        self.set_state(
            self.entity,
            state=str(sum(1 for s in shown if s["end"] is None)),
            attributes={"friendly_name": "House scenarios", "icon": "mdi:timeline-text-outline", "scenarios": shown},
            replace=True,
        )

    def _save(self):
        try:
            self.state_file.write_text(json.dumps({"rows": list(self.rows.values())}))
        except OSError as e:
            self.log(f"Saving {self.state_file.name} failed: {e}", level="WARNING")

    def _publish_backfill(self):
        entity = self.args.get("backfill_entity")
        if not entity:
            return
        path = Path(self.args.get("backfill_file") or Path(__file__).with_name("house_scenarios_backfill.json"))
        try:
            scenarios = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        scenarios = [s for s in scenarios if isinstance(s, dict) and s.get("id") and s.get("start")]
        self.set_state(entity, state=str(len(scenarios)), replace=True,
                       attributes={"friendly_name": "House scenarios backfill", "icon": "mdi:history",
                                   "scenarios": scenarios})
