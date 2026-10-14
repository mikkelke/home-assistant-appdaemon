# tests/test_house_upcoming.py - HouseUpcoming end to end on a fake AppDaemon surface, with the real
# house_upcoming.yaml: lease, state acquisition, restart behaviour, the daily push, rain fetches,
# the history job and the battery inventory.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPS / "home_pulse"))
sys.path.insert(0, str(APPS / "climate"))
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for _name, _mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(_name, _mod)

import house_upcoming as hu  # noqa: E402
import upcoming_core as core  # noqa: E402
import upcoming_maint as maint  # noqa: E402

CONFIG = yaml.safe_load((APPS / "home_pulse" / "house_upcoming.yaml").read_text())["HouseUpcoming"]
NOW = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)

# Every battery sensor HA had on 14.10.2026.
LIVE_BATTERIES = [device["entity"] for device in CONFIG["batteries"]["devices"]] + list(CONFIG["batteries"]["excluded"])


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def full(state, attributes=None, changed=None):
    return {"state": state, "attributes": dict(attributes or {}), "last_changed": iso(changed or NOW - 30 * 24 * HOUR)}


def maybe_async(value):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return value

    async def wrap():
        return value

    return wrap()


class Notifier:
    def __init__(self):
        self.sent = []
        self.deliver = 1

    async def notify(self, **kwargs):
        self.sent.append(kwargs)
        return self.deliver


def base_states():
    states = {entity: full("100", {"device_class": "battery"}) for entity in LIVE_BATTERIES}
    for part in ("filter", "main_brush", "side_brush", "sensor"):
        states[f"sensor.rober2_{part}_time_left"] = full("500000")
    states["sensor.washer_state"] = full("Off", {"last_drum_clean": iso(NOW - 3 * 24 * HOUR)})
    states["sensor.transit_kbh_ic_odense"] = full("OK", {"departures": []})
    states["input_select.transit_carlsberg_stog_kbh_status"] = full("OK")
    states["person.kristine"] = full("not_home")
    states["binary_sensor.weather_opening_alert_rooftop_rain"] = full("off")
    states["binary_sensor.rooftop_door_1_contact"] = full("off")
    states["binary_sensor.rooftop_door_2_contact"] = full("off")
    return states


def make_app(states=None, state_dir=None, now=NOW):
    app = hu.HouseUpcoming.__new__(hu.HouseUpcoming)
    state_dir = state_dir or tempfile.mkdtemp(prefix="house_upcoming_")
    app.args = {**CONFIG, "state_file": f"{state_dir}/state.json", "fixture_file": f"{state_dir}/fixtures.jsonl"}
    app.now = now
    app.states = states if states is not None else base_states()
    app.published, app.logs, app.scheduled, app.tasks, app.services, app.listened = [], [], [], [], [], []
    app.history = {}
    app.forecast_response = None
    app.fail_publish = 0
    app.notifier = Notifier()

    def get_state(entity, attribute=None, **kw):
        if "." not in entity:
            return {e: s for e, s in app.states.items() if e.startswith(entity + ".")}
        value = app.states.get(entity)
        if attribute == "all":
            return value
        return None if value is None else value["state"]

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        if app.fail_publish:
            app.fail_publish -= 1
            raise RuntimeError("HA unreachable")
        app.published.append({"entity": entity, "state": state, "attributes": attributes, "replace": replace})

    def run_in(callback, delay, **kw):
        app.scheduled.append((callback, delay, kw))
        return maybe_async("handle")

    def call_service(service, **kw):
        app.services.append((service, kw))
        return maybe_async(app.forecast_response)

    def get_history(entity_id=None, start_time=None, end_time=None, **kw):
        return maybe_async([list(app.history.get(entity_id, []))])

    app.get_state = get_state
    app.set_state = set_state
    app.run_in = run_in
    app.call_service = call_service
    app.get_history = get_history
    app.get_now = lambda: maybe_async(app.now)
    app.get_app = lambda name: app.notifier
    app.create_task = lambda coro: app.tasks.append(coro)
    app.listen_state = lambda cb, entity, **kw: app.listened.append((entity, kw))
    app.listen_event = lambda *a, **kw: None
    app.run_every = lambda *a, **kw: None
    app.run_daily = lambda *a, **kw: None
    app.log = lambda msg, level="INFO", **kw: app.logs.append((level, msg))
    app.initialize()
    return app


def drain(app):
    """Runs the queued coroutines, then the zero-delay callbacks they handed back, until idle."""
    while app.tasks or any(delay == 0 for _, delay, _ in app.scheduled):
        while app.tasks:
            asyncio.run(app.tasks.pop(0))
        ready = [item for item in app.scheduled if item[1] == 0]
        app.scheduled = [item for item in app.scheduled if item[1] != 0]
        for callback, _, kw in ready:
            callback(kw)


def fire(app, entity, new):
    old = app.states.get(entity)
    app.states[entity] = new
    app._on_source(entity, "all", old, new, {})


def tick(app, delta=5 * MIN):
    app.now += delta
    app._on_tick({})


def last(app):
    return app.published[-1]


def rows(app):
    return {row["id"]: row for row in last(app)["attributes"]["rows"]}


class PublishTests(unittest.TestCase):
    def test_initialize_publishes_string_only_ascii_rows_with_a_lease(self):
        states = base_states()
        states["sensor.rober2_filter_time_left"] = full("-19902")
        states["sensor.washer_state"] = full("Off", {"last_drum_clean": ""})
        app = make_app(states)
        published = last(app)
        self.assertEqual(published["entity"], "sensor.house_upcoming")
        self.assertTrue(published["replace"])
        attrs = published["attributes"]
        self.assertEqual(attrs["expires"], iso(NOW + 15 * MIN).replace("+00:00", "+00:00"))
        self.assertEqual(set(rows(app)), {"maint:rober2:filter", "maint:washer:drum"})
        for row in attrs["rows"]:
            for value in row.values():
                ok = isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))
                self.assertTrue(ok and json.dumps(value).isascii(), row)
            self.assertEqual(row["audience"], "admin")
        self.assertEqual(published["state"], core.utc_iso(NOW))

    def test_the_lease_is_renewed_every_tick_while_nothing_changes(self):
        app = make_app()
        first = last(app)
        app._on_source("person.kristine", "all", app.states["person.kristine"], app.states["person.kristine"], {})
        self.assertEqual(len(app.published), 1)
        tick(app)
        self.assertEqual(len(app.published), 2)
        self.assertEqual(last(app)["attributes"]["expires"], core.utc_iso(NOW + 20 * MIN))
        self.assertEqual(last(app)["state"], first["state"])
        self.assertEqual(last(app)["attributes"]["rows"], first["attributes"]["rows"])

    def test_a_failed_publish_is_retried_by_the_next_tick(self):
        app = make_app()
        app.fail_publish = 1
        fire(app, "sensor.rober2_sensor_time_left", full("-5"))
        self.assertNotIn("maint:rober2:sensor", rows(app))
        tick(app)
        self.assertIn("maint:rober2:sensor", rows(app))

    def test_a_reload_without_events_publishes_the_same_rows_and_state(self):
        states = base_states()
        states["sensor.rober2_filter_time_left"] = full("-19902")
        app = make_app(states)
        tick(app, HOUR)
        reloaded = make_app(states, state_dir=str(Path(app.state_file).parent), now=app.now)
        self.assertEqual(last(reloaded)["attributes"]["rows"], last(app)["attributes"]["rows"])
        self.assertEqual(last(reloaded)["state"], last(app)["state"])

    def test_plugin_started_republishes(self):
        app = make_app()
        app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(app.published), 2)


class AcquisitionTests(unittest.TestCase):
    def test_an_attribute_only_change_reaches_the_rows(self):
        app = make_app()
        self.assertNotIn("maint:washer:drum", rows(app))
        washer = app.states["sensor.washer_state"]
        fire(app, "sensor.washer_state", {**washer, "attributes": {"last_drum_clean": iso(NOW - 40 * 24 * HOUR)}})
        self.assertEqual(rows(app)["maint:washer:drum"]["detail"], "Bomuld 90 monthly, last 04.09")
        self.assertIn(("sensor.washer_state", {"attribute": "all"}), app.listened)

    def test_washer_state_without_the_attribute_keeps_the_last_value_seen(self):
        app = make_app()
        fire(app, "sensor.washer_state", full("Off"))
        self.assertNotIn("maint:washer:drum", rows(app))
        app.states.pop("sensor.washer_state")
        app._on_source("sensor.washer_state", "all", None, None, {})
        self.assertNotIn("maint:washer:drum", rows(app))

    def test_a_value_from_a_callback_wins_over_a_stale_read(self):
        states = base_states()
        states["sensor.dining_room_window_1_battery"] = full("unavailable", changed=NOW - 2 * 24 * HOUR)
        app = make_app(states)
        fire(app, "sensor.dining_room_window_1_battery", full("81", changed=NOW))
        app.states["sensor.dining_room_window_1_battery"] = full("unavailable", changed=NOW - 2 * 24 * HOUR)
        tick(app, 30 * HOUR)
        self.assertNotIn("maint:battery:sensor.dining_room_window_1_battery", rows(app))

    def test_an_offline_transition_is_dated_by_the_new_state(self):
        app = make_app()
        went = NOW - 2 * HOUR
        fire(app, "sensor.bathroom_door_battery", full("unavailable", changed=went))
        self.assertNotIn("maint:battery:sensor.bathroom_door_battery", rows(app))
        tick(app, 23 * HOUR)
        row = rows(app)["maint:battery:sensor.bathroom_door_battery"]
        self.assertEqual(row["detail"], "no data since 14.10, CR2477")
        self.assertEqual((row["area"], row["icon"]), ("bathroom", "mdi:lan-disconnect"))


class PushTests(unittest.TestCase):
    def setUp(self):
        states = base_states()
        states["sensor.rober2_filter_time_left"] = full("-19902")
        states["sensor.washer_state"] = full("Off", {"last_drum_clean": ""})
        states["sensor.bathroom_door_battery"] = full("18")
        states["sensor.kitchen_smoke_alarm_battery"] = full("15")
        states["sensor.bedroom_thermostat_battery_level"] = full("40")
        self.app = make_app(states)
        tick(self.app, 2 * HOUR)

    def push(self):
        self.app._on_push_time({})
        drain(self.app)

    def test_one_push_to_mikkel_without_the_conditions_other_apps_push(self):
        self.push()
        self.assertEqual(len(self.app.notifier.sent), 1)
        sent = self.app.notifier.sent[0]
        self.assertEqual((sent["title"], sent["message"]), ("Maintenance due", "Rober2 filter, Bathroom door"))
        self.assertEqual((sent["target"], sent["link"]), (["mikkel"], "/dashboard-rooftop/home#now"))
        shown = rows(self.app)
        for entity in ("sensor.kitchen_smoke_alarm_battery", "sensor.bedroom_thermostat_battery_level"):
            self.assertEqual(shown[f"maint:battery:{entity}"]["audience"], "admin")
        self.assertIn("maint:washer:drum", shown)

    def test_not_again_for_the_same_episodes(self):
        self.push()
        for _ in range(8):
            tick(self.app, 24 * HOUR)
            self.push()
        self.assertEqual(len(self.app.notifier.sent), 1)

    def test_an_undelivered_push_is_retried_the_next_day(self):
        self.app.notifier.deliver = 0
        self.push()
        self.app.notifier.deliver = 1
        tick(self.app, 24 * HOUR)
        self.push()
        self.assertEqual([s["message"] for s in self.app.notifier.sent], ["Rober2 filter, Bathroom door"] * 2)

    def test_a_new_episode_after_a_battery_change_pushes_again(self):
        self.push()
        fire(self.app, "sensor.bathroom_door_battery", full("100"))
        tick(self.app, 3 * HOUR)
        fire(self.app, "sensor.bathroom_door_battery", full("15"))
        tick(self.app, 3 * HOUR)
        self.push()
        self.assertEqual(self.app.notifier.sent[-1]["message"], "Bathroom door")


class RainTests(unittest.TestCase):
    def forecast(self, mm):
        start = NOW.replace(minute=0)
        items = [{"datetime": iso(start + i * HOUR), "temperature": 15.0, "precipitation": v} for i, v in enumerate(mm)]
        return {"result": {"response": {"weather.forecast_home": {"forecast": items}}}}

    def test_fetched_only_while_a_door_has_stood_open_ten_minutes(self):
        app = make_app()
        tick(app, HOUR)
        self.assertEqual(app.services, [])
        fire(app, "binary_sensor.rooftop_door_1_contact", full("on", changed=app.now))
        tick(app, 9 * MIN)
        self.assertEqual(app.services, [])
        app.forecast_response = self.forecast([0, 0, 0.6, 1.0])
        tick(app, MIN)
        drain(app)
        self.assertEqual([s[0] for s in app.services], ["weather/get_forecasts"])
        self.assertTrue(any("RAIN fetch" in msg for _, msg in app.logs))
        row = rows(app)["rain:terrace"]
        self.assertEqual((row["detail"], row["area"]), ("living-room terrace door open", "living_room"))
        lines = Path(app.fixture_file).read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["row_at"], row["at"])
        fire(app, "binary_sensor.rooftop_door_1_contact", full("off", changed=app.now))
        self.assertNotIn("rain:terrace", rows(app))
        tick(app, HOUR)
        self.assertEqual(len(app.services), 1)


class TrainTests(unittest.TestCase):
    def test_kristines_morning_train(self):
        morning = datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)
        states = base_states()
        states["person.kristine"] = full("home")
        departures = [{"ts": int((morning + 46 * MIN).timestamp()), "time": "07:46", "scheduled_time": "07:46"}]
        as_of = (morning - MIN).astimezone(maint.HOUSE_TZ).replace(tzinfo=None).isoformat()
        states["sensor.transit_kbh_ic_odense"] = full("OK", {"departures": departures, "data_as_of": as_of})
        app = make_app(states, now=morning)
        row = rows(app)["train:kristine"]
        self.assertEqual((row["detail"], row["audience_users"]), ("07:46", ["Kristine"]))
        tick(app, 12 * MIN)
        self.assertNotIn("train:kristine", rows(app))


class HistoryTests(unittest.TestCase):
    def test_the_history_job_dates_an_outage_and_adds_age_facts(self):
        states = base_states()
        states["sensor.dining_room_window_1_battery"] = full("unavailable", changed=NOW - HOUR)
        app = make_app(states)
        onset = datetime(2026, 7, 12, 12, 36, 39, tzinfo=timezone.utc)
        series = [{"state": "100", "last_changed": iso(onset - 300 * 24 * HOUR)},
                  {"state": "30", "last_changed": iso(onset - 280 * 24 * HOUR)},
                  {"state": "95", "last_changed": iso(onset - 270 * 24 * HOUR)},
                  {"state": "52", "last_changed": iso(onset - 10 * 24 * HOUR)},
                  {"state": "unavailable", "last_changed": iso(onset)},
                  {"state": "unavailable", "last_changed": iso(NOW - HOUR)}]
        app.history["sensor.dining_room_window_1_battery"] = series
        drain(app)
        self.assertEqual(app.scheduled[0][1], hu.HISTORY_DELAY_S)
        asyncio.run(app._history_job({}))
        drain(app)
        row = rows(app)["maint:battery:sensor.dining_room_window_1_battery"]
        self.assertEqual(row["detail"], "no data since 12.07, recharge, in since 15.10.25")

    def test_a_newer_recorder_state_replaces_a_stale_store(self):
        states = base_states()
        states["sensor.dining_room_window_1_battery"] = full("unavailable", changed=NOW - 3 * 24 * HOUR)
        app = make_app(states)
        app.history["sensor.dining_room_window_1_battery"] = [
            {"state": "unavailable", "last_changed": iso(NOW - 3 * 24 * HOUR)},
            {"state": "81", "last_changed": iso(NOW - HOUR)},
        ]
        asyncio.run(app._history_job({}))
        drain(app)
        tick(app, 30 * HOUR)
        self.assertNotIn("maint:battery:sensor.dining_room_window_1_battery", rows(app))


class InventoryTests(unittest.TestCase):
    INV = maint.parse_inventory(CONFIG["batteries"])

    def test_forty_nine_devices_and_nine_exclusions_cover_every_live_battery_sensor(self):
        self.assertEqual((len(self.INV["devices"]), len(self.INV["excluded"])), (49, 9))
        self.assertEqual(maint.unlisted_batteries(LIVE_BATTERIES, self.INV), [])
        self.assertEqual({d["model"] for d in self.INV["devices"].values()}, set(self.INV["models"]))

    def test_start_up_names_unlisted_battery_sensors(self):
        quiet = make_app()
        self.assertIn(("INFO", "Battery inventory complete: 49 listed, 9 excluded"), quiet.logs)
        states = base_states()
        states["sensor.new_door_battery"] = full("100", {"device_class": "battery"})
        loud = make_app(states)
        self.assertIn(("WARNING", "Battery sensors neither listed nor excluded: sensor.new_door_battery"), loud.logs)

    def test_rooms_names_and_cell_types(self):
        areas = {"bathroom", "bedroom", "dining_room", "guest_bathroom", "hallway", "kitchen", "kristines_room",
                 "living_room", "office", "rooftop"}
        for entity, device in self.INV["devices"].items():
            self.assertIn(device["area"], areas, entity)
            self.assertEqual(core.clean_text(device["name"]), device["name"])
        cells = {key: model["cell"] for key, model in self.INV["models"].items()}
        self.assertEqual(cells, {
            "zg204zm": "LADDA AAA NiMH", "zg204zk": "LADDA AAA NiMH", "parasoll": "1x LADDA AAA NiMH",
            "sq610rfnh": "built-in rechargeable", "yale": "lithium AA (rechargeable)", "blind": "", "ws90": "2x AA",
            "twinguard": "6x AA alkaline", "fp300": "2x CR2450", "snzb04p": "CR2477", "snzb02p": "CR2477",
            "zg102zm": "",
        })
        actions = {key: model["action"] for key, model in self.INV["models"].items()}
        self.assertEqual({k for k, a in actions.items() if a == "charge"}, {"sq610rfnh", "blind"})
        self.assertEqual({k for k, a in actions.items() if a == "recharge"}, {"zg204zm", "zg204zk", "parasoll", "yale"})
        self.assertEqual(self.INV["devices"]["sensor.yale_battery"]["history_from"].isoformat(), "2026-02-22")
        self.assertEqual(self.INV["devices"]["sensor.ecowitt_battery_8351"]["history_from"].isoformat(), "2026-08-30")

    def test_the_ws90_backup_cells_are_one_removable_line(self):
        text = (APPS / "home_pulse" / "house_upcoming.yaml").read_text()
        lines = [line for line in text.splitlines() if "ecowitt_battery_8351" in line]
        self.assertEqual(len(lines), 1)
        without = yaml.safe_load(text.replace(lines[0] + "\n", ""))["HouseUpcoming"]["batteries"]
        self.assertEqual(len(maint.parse_inventory(without)["devices"]), 48)

    def test_one_owner_per_condition_agrees_with_the_owner_apps(self):
        salus = yaml.safe_load((APPS / "climate" / "salus_health.yaml").read_text())["SalusHealth"]
        fire_safety = yaml.safe_load((APPS / "safety" / "fire_safety.yaml").read_text())["FireSafety"]
        models = self.INV["models"]
        self.assertEqual(models["sq610rfnh"]["low"], salus["battery_warn_percent"])
        self.assertEqual(models["sq610rfnh"]["pushed_by"], "salus_health")
        self.assertEqual(models["twinguard"]["pushed_by"], "fire_safety")
        for pct in range(0, 101):
            self.assertEqual(pct <= models["twinguard"]["low"], pct < fire_safety["battery_low_pct"], pct)


if __name__ == "__main__":
    unittest.main()
