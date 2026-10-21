# tests/test_house_upcoming.py - HouseUpcoming end to end on a fake AppDaemon surface, with the real
# house_upcoming.yaml: lease, state acquisition, restart behaviour, the daily push, rain fetches,
# the drum clean's cheapest start, the run-based appliance tasks (the feedback ledgers, the seed, Done
# and undo), the history job, replace-by days, Rober2's dated parts, the plants' watering rows (by sensor
# and by the calendar), the one-off tasks and the battery inventory.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPS / "home_pulse"))
sys.path.insert(0, str(APPS / "climate"))
sys.path.insert(0, str(APPS / "appliances"))
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
import upcoming_guides as guides  # noqa: E402
import upcoming_maint as maint  # noqa: E402
import upcoming_once as once  # noqa: E402
import upcoming_plants as plants  # noqa: E402

CONFIG = yaml.safe_load((APPS / "home_pulse" / "house_upcoming.yaml").read_text())["HouseUpcoming"]
PLANTS = plants.parse_plants(CONFIG["plants"])
TIMED = plants.parse_timed(CONFIG["plants"])
ONCE = once.parse_once(CONFIG["once"])
SAMPLE_PLANT = {"moisture": "sensor.strelitzia_moisture", "name": "Strelitzia", "area": "dining_room", "dry": 15}
SAMPLE_TIMED = {"name": "Monstera", "area": "kitchen", "water": 1, "every_days": 10}
SAMPLE_ONCE = {"due": date(2026, 10, 24), "title": "Check the fern", "detail": "leaves clean?", "area": "bathroom",
               "icon": "mdi:bug-outline"}
MOISTURE = SAMPLE_PLANT["moisture"]
AREAS = {"bathroom", "bedroom", "dining_room", "guest_bathroom", "hallway", "kitchen", "kristines_room",
         "living_room", "office", "rooftop"}
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
    states["sensor.washer_state"] = full("Off")
    states["sensor.transit_kbh_ic_odense"] = full("OK", {"departures": []})
    states["input_select.transit_carlsberg_stog_kbh_status"] = full("OK")
    states["person.kristine"] = full("not_home")
    states["binary_sensor.weather_opening_alert_rooftop_rain"] = full("off")
    states["binary_sensor.rooftop_door_1_contact"] = full("off")
    states["binary_sensor.rooftop_door_2_contact"] = full("off")
    states["zone.home"] = full("1", {"persons": ["person.kristine"]})
    for entity, plant in PLANTS.items():
        states[entity] = full(f"{plant['dry'] + 20:g}")
        if plant["conductivity"]:
            states[plant["conductivity"]] = full("800")
    return states


def dish_run(end, **extra):
    return {"ts": iso(end), "duration_min": 230.0, "energy_kwh": 0.75, "predicted": "eco", "confirmed": "eco",
            "programme_confirmed_by_human": False, **extra}


def wash_run(end, **extra):
    return {"ts": iso(end), "duration_min": 150.0, "energy_kwh": 1.2, "confirmed": "bomuld",
            "confirmed_temperature": "60", "programme_user_confirmed": False, "completion_class": "completed", **extra}


def dryer_run(end, **extra):
    return {"ts": iso(end), "duration_min": 90.0, "energy_kwh": 1.0, "predicted": "bomuld__skabstoert",
            "confirmed": "bomuld__skabstoert", "programme_confirmed_by_human": False, **extra}


def drum_clean_run(end):
    return wash_run(end, confirmed="drum_clean", confirmed_temperature=None, programme_user_confirmed=True)


RUN_MAKERS = {"washer": (wash_run, 60), "dishwasher": (dish_run, 50), "dryer": (dryer_run, 50)}


def every_two_days(name, last_end=None, count=None):
    """The ledger of `name`, one run every two days (3.5 a week), the newest ending a day before `last_end`."""
    make, default_count = RUN_MAKERS[name]
    last_end = (last_end or NOW) - 24 * HOUR
    return [make(last_end - 48 * HOUR * i) for i in range(((count or default_count) - 1), -1, -1)]


_ledger_clock = [0]


def write_ledger(state_dir, name, cycles):
    """The monitor's feedback file for `name`; None removes it. Each write gets a later mtime, so a
    rewrite within one clock tick of the last still reads as a change."""
    path = Path(state_dir) / f"{name}_feedback.json"
    if cycles is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps({"version": 2, "cycles": cycles}))
    _ledger_clock[0] += 1
    stamp = 1_700_000_000 + _ledger_clock[0] * 10
    os.utime(path, (stamp, stamp))


def make_app(states=None, state_dir=None, now=NOW, ledgers=None, **args):
    app = hu.HouseUpcoming.__new__(hu.HouseUpcoming)
    state_dir = state_dir or tempfile.mkdtemp(prefix="house_upcoming_")
    for name in RUN_MAKERS:
        if name in (ledgers or {}):
            write_ledger(state_dir, name, ledgers[name])
        elif not (Path(state_dir) / f"{name}_feedback.json").exists():
            write_ledger(state_dir, name, every_two_days(name, now))
    app.args = {**CONFIG, "plants": [], "once": [], "state_file": f"{state_dir}/state.json",
                "fixture_file": f"{state_dir}/fixtures.jsonl", "done_log_file": f"{state_dir}/done_log.jsonl", **args}
    for name in RUN_MAKERS:
        app.args[name] = {**(CONFIG.get(name) or {}), **(args.get(name) or {}),
                          "feedback_file": f"{state_dir}/{name}_feedback.json"}
    app.args["coffee"] = {**(args.get("coffee") or {}), "feedback_file": f"{state_dir}/coffee_feedback.json"}
    if "coffee" in (ledgers or {}):
        write_ledger(state_dir, "coffee", ledgers["coffee"])
    app.state_dir = state_dir
    app.now = now
    app.states = states if states is not None else base_states()
    app.published, app.logs, app.scheduled, app.tasks, app.services, app.listened = [], [], [], [], [], []
    app.events = []
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
    app.listen_event = lambda callback, event_name=None, **kw: app.events.append((callback.__name__, event_name))
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


def plant_rows(app, among):
    """The rows of the plants in `among` (the dicts parse_plants or parse_timed gives, by value)."""
    ids = {f"maint:plant:{plant['slug']}" for plant in among}
    return {key: row for key, row in rows(app).items() if key in ids}


class PublishTests(unittest.TestCase):
    def test_initialize_publishes_string_only_ascii_rows_with_a_lease(self):
        states = base_states()
        states["sensor.rober2_filter_time_left"] = full("-19902")
        app = make_app(states)
        published = last(app)
        self.assertEqual(published["entity"], "sensor.house_upcoming")
        self.assertTrue(published["replace"])
        attrs = published["attributes"]
        self.assertEqual(attrs["expires"], iso(NOW + 15 * MIN).replace("+00:00", "+00:00"))
        self.assertEqual(set(rows(app)), {"maint:rober2:filter", "maint:rober2:main_brush", "maint:rober2:side_brush",
                                          "maint:rober2:sensor", "maint:washer:drum", "maint:washer:inlet",
                                          "maint:dishwasher:hygiene", "maint:dishwasher:filters",
                                          "maint:dishwasher:check", "maint:dryer:plinth"})
        self.assertEqual(rows(app)["maint:rober2:filter"]["detail"], "replace")
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
        self.assertTrue(rows(app)["maint:rober2:sensor"]["detail"].startswith("clean by ~"))
        tick(app)
        self.assertEqual(rows(app)["maint:rober2:sensor"]["detail"], "clean")

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
    def test_a_drum_clean_confirmed_in_the_ledger_reaches_the_row_at_once_through_the_washer_entity(self):
        app = make_app()
        self.assertEqual(rows(app)["maint:washer:drum"]["detail"], "Bomuld 90 + drain filter, door seal, due, last unknown")
        cycles = every_two_days("washer", NOW)
        cycles[-4] = drum_clean_run(NOW - 7 * 24 * HOUR)
        write_ledger(app.state_dir, "washer", cycles)
        washer = app.states["sensor.washer_state"]
        fire(app, "sensor.washer_state", {**washer, "attributes": {"last_drum_clean": iso(NOW - 7 * 24 * HOUR)}})
        row = rows(app)["maint:washer:drum"]
        self.assertTrue(row["detail"].startswith("Bomuld 90 + drain filter, door seal, 12 runs left, ~"), row["detail"])
        self.assertIn("at", row)
        self.assertIn(("sensor.washer_state", {"attribute": "all"}), app.listened)

    def test_the_drum_row_does_not_need_the_washer_entity(self):
        app = make_app()
        app.states.pop("sensor.washer_state")
        app._on_source("sensor.washer_state", "all", None, None, {})
        self.assertIn("maint:washer:drum", rows(app))

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
        self.assertEqual((sent["title"], sent["message"]), ("Maintenance due", "Rober2 filter, Bathroom door battery"))
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
        self.assertEqual([s["message"] for s in self.app.notifier.sent], ["Rober2 filter, Bathroom door battery"] * 2)

    def test_a_new_episode_after_a_battery_change_pushes_again(self):
        self.push()
        fire(self.app, "sensor.bathroom_door_battery", full("100"))
        tick(self.app, 3 * HOUR)
        fire(self.app, "sensor.bathroom_door_battery", full("15"))
        tick(self.app, 3 * HOUR)
        self.push()
        self.assertEqual(self.app.notifier.sent[-1]["message"], "Bathroom door battery")


class RainTests(unittest.TestCase):
    def forecast(self, mm):
        start = NOW.replace(minute=0)
        items = [{"datetime": iso(start + i * HOUR), "temperature": 15.0, "precipitation": v} for i, v in enumerate(mm)]
        return {"result": {"response": {"weather.forecast_home": {"forecast": items}}}}

    def away(self):
        """An app with nobody home, so a forecast row looks three hours ahead."""
        states = base_states()
        states["zone.home"] = full("0", {"persons": []})
        return make_app(states)

    def test_fetched_only_while_a_door_has_stood_open_ten_minutes(self):
        app = self.away()
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
        row = rows(app)["rain:living_room"]
        self.assertEqual((row["title"], row["detail"], row["area"]),
                         ("Rain forecast", "living-room terrace door open", "living_room"))
        lines = Path(app.fixture_file).read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["row_at"], row["at"])
        fire(app, "binary_sensor.rooftop_door_1_contact", full("off", changed=app.now))
        self.assertNotIn("rain:living_room", rows(app))
        tick(app, HOUR)
        self.assertEqual(len(app.services), 1)

    def test_with_both_doors_open_each_has_a_forecast_row_in_its_own_room(self):
        app = self.away()
        app.forecast_response = self.forecast([0, 0, 0.6, 1.0])
        for door in ("binary_sensor.rooftop_door_1_contact", "binary_sensor.rooftop_door_2_contact"):
            fire(app, door, full("on", changed=app.now))
        tick(app, 10 * MIN)
        drain(app)
        shown = {key: row for key, row in rows(app).items() if key.startswith("rain:")}
        self.assertEqual({key: (row["detail"], row["area"]) for key, row in shown.items()}, {
            "rain:living_room": ("living-room terrace door open", "living_room"),
            "rain:office": ("Claudia's terrace door open", "office")})
        self.assertEqual(shown["rain:living_room"]["at"], shown["rain:office"]["at"])
        self.assertEqual(len(Path(app.fixture_file).read_text().splitlines()), 1)
        fire(app, "binary_sensor.rooftop_door_1_contact", full("off", changed=app.now))
        self.assertEqual([key for key in rows(app) if key.startswith("rain:")], ["rain:office"])

    def open_door_with_rain(self, app, first_mm_hour):
        """Door 1 open for ten minutes and a forecast whose rain starts `first_mm_hour` hours after 10:00."""
        mm = [0] * first_mm_hour + [1.0]
        app.forecast_response = self.forecast(mm)
        fire(app, "binary_sensor.rooftop_door_1_contact", full("on", changed=app.now))
        tick(app, 10 * MIN)
        drain(app)

    def test_while_somebody_is_home_the_forecast_looks_an_hour_ahead_so_rain_in_eighty_minutes_waits(self):
        near = make_app()
        self.open_door_with_rain(near, 1)
        self.assertEqual(rows(near)["rain:living_room"]["at"], "2026-10-14T11:00:00+00:00")
        far = make_app()
        self.open_door_with_rain(far, 2)
        self.assertNotIn("rain:living_room", rows(far))
        line = json.loads(Path(far.fixture_file).read_text().splitlines()[0])
        self.assertEqual((line["ahead_min"], line["row_at"]), (60, None))

    def test_when_the_last_person_leaves_the_longer_horizon_brings_the_row_and_an_arrival_takes_it_away(self):
        app = make_app()
        self.open_door_with_rain(app, 2)
        self.assertNotIn("rain:living_room", rows(app))
        fire(app, "zone.home", full("0", {"persons": []}))
        self.assertEqual(rows(app)["rain:living_room"]["at"], "2026-10-14T12:00:00+00:00")
        fire(app, "zone.home", full("1", {"persons": ["person.kristine"]}))
        self.assertNotIn("rain:living_room", rows(app))
        fire(app, "zone.home", full("unavailable"))
        self.assertNotIn("rain:living_room", rows(app))
        fire(app, "zone.home", full("0", {"persons": []}))
        self.assertIn("rain:living_room", rows(app))

    def test_both_horizons_are_config_values_and_the_shipped_ones_are_one_hour_and_longer_for_nobody(self):
        shipped = CONFIG["rain"]
        self.assertEqual(shipped["home"], "zone.home")
        self.assertTrue(0 < shipped["horizon_home_hours"] < shipped["horizon_away_hours"])
        app = make_app(rain={**shipped, "horizon_home_hours": 2.5, "horizon_away_hours": 0.5})
        self.open_door_with_rain(app, 2)
        self.assertIn("rain:living_room", rows(app))
        fire(app, "zone.home", full("0", {"persons": []}))
        self.assertNotIn("rain:living_room", rows(app))
        self.assertIn(("zone.home", {"attribute": "all"}), app.listened)

    def test_while_the_rooftop_rain_alert_is_on_an_open_door_keeps_a_row_that_says_to_close_it(self):
        alert = "binary_sensor.weather_opening_alert_rooftop_rain"
        app = self.away()
        app.forecast_response = self.forecast([0, 0, 0.6, 1.0])
        fire(app, "binary_sensor.rooftop_door_1_contact", full("on", changed=app.now))
        tick(app, 10 * MIN)
        drain(app)
        self.assertEqual(rows(app)["rain:living_room"]["title"], "Rain forecast")
        fire(app, alert, full("on", changed=app.now))
        row = rows(app)["rain:living_room"]
        self.assertEqual((row["title"], row["area"], row["at"], row["kind"]),
                         ("Raining, close the living-room terrace door", "living_room",
                          core.utc_iso(app.now.replace(minute=0, second=0)), "rain"))
        self.assertTrue(core.is_valid_row(row))
        changed = last(app)["state"]
        tick(app, 5 * MIN)
        self.assertEqual((rows(app)["rain:living_room"], last(app)["state"]), (row, changed))
        fire(app, "binary_sensor.rooftop_door_2_contact", full("on", changed=app.now))
        self.assertEqual(rows(app)["rain:office"]["title"], "Raining, close the Claudia's terrace door")
        fire(app, "binary_sensor.rooftop_door_1_contact", full("off", changed=app.now))
        self.assertEqual([key for key in rows(app) if key.startswith("rain:")], ["rain:office"])
        fire(app, "binary_sensor.rooftop_door_2_contact", full("off", changed=app.now))
        self.assertEqual([key for key in rows(app) if key.startswith("rain:")], [])

    def test_the_alert_with_every_door_shut_and_the_alert_going_off_leave_the_forecast_or_nothing(self):
        alert = "binary_sensor.weather_opening_alert_rooftop_rain"
        app = make_app()
        fire(app, alert, full("on", changed=app.now))
        self.assertEqual([key for key in rows(app) if key.startswith("rain:")], [])
        fire(app, "binary_sensor.rooftop_door_2_contact", full("on", changed=app.now))
        self.assertEqual(rows(app)["rain:office"]["title"], "Raining, close the Claudia's terrace door")
        fire(app, alert, full("off", changed=app.now))
        self.assertEqual([key for key in rows(app) if key.startswith("rain:")], [])


class DrumStartTests(unittest.TestCase):
    """At 12:30 on 14.10, before and after the next day's prices are out; Bomuld 60 stands in until a
    drum clean profile is published."""

    BOMULD_60 = {"key": "bomuld_60", "label": "Bomuld 60", "n": 25, "p": ",".join(["100"] * 120)}
    DRUM_CLEAN = {"key": "drum_clean", "label": "Drum clean", "n": 5, "p": ",".join(["250"] * 60)}

    @staticmethod
    def profiles(*programmes):
        return full("x", {"schema": 1, "appliances": {"washer": {"programmes": list(programmes)}}})

    @staticmethod
    def prices(today, tomorrow, valid):
        def points(day, cheap):
            return [{"hour": datetime(2026, 10, day, hour, tzinfo=maint.HOUSE_TZ).isoformat(),
                     "price": 0.5 if hour in cheap else 2.0} for hour in range(24)]
        return full("2.0", {"raw_today": points(14, today), "raw_tomorrow": points(15, tomorrow),
                            "tomorrow_valid": valid})

    def states(self, *programmes):
        states = base_states()
        states["sensor.appliance_run_profiles"] = self.profiles(*(programmes or (self.BOMULD_60,)))
        return states

    def test_the_row_moves_to_tomorrow_once_its_prices_are_out(self):
        states = self.states()
        states["sensor.energi_data_service"] = self.prices({20, 21}, {3, 4}, False)
        app = make_app(states)
        row = rows(app)["maint:washer:drum"]
        self.assertEqual((row["at"], row["detail"]),
                         ("2026-10-14T18:00:00+00:00", "Bomuld 90 + drain filter, door seal, saves 1,80 kr vs now"))
        self.assertNotIn("next", row)
        self.assertIn(("sensor.energi_data_service", {"attribute": "all"}), app.listened)
        fire(app, "sensor.energi_data_service", self.prices({20}, {3, 4}, True))
        row = rows(app)["maint:washer:drum"]
        self.assertEqual((row["at"], row["detail"]),
                         ("2026-10-15T01:00:00+00:00", "Bomuld 90 + drain filter, door seal, saves 1,80 kr vs now"))

    def test_a_published_drum_clean_profile_prices_the_row(self):
        states = self.states(self.BOMULD_60, self.DRUM_CLEAN)
        states["sensor.energi_data_service"] = self.prices({20, 21}, {}, False)
        row = rows(make_app(states))["maint:washer:drum"]
        self.assertEqual((row["at"], row["detail"]),
                         ("2026-10-14T18:00:00+00:00", "Bomuld 90 + drain filter, door seal, saves 2,25 kr vs now"))

    def test_cheapest_now_times_the_row_at_now_and_out_of_the_next_chip(self):
        states = self.states()
        states["sensor.energi_data_service"] = self.prices({12, 13, 14}, {}, False)
        app = make_app(states)
        row = rows(app)["maint:washer:drum"]
        self.assertEqual((row["at"], row["detail"], row["next"]),
                         (core.utc_iso(NOW), "Bomuld 90 + drain filter, door seal, cheapest now, due", "skip"))
        tick(app, HOUR)
        row = rows(app)["maint:washer:drum"]
        self.assertEqual((row["at"], row["next"]), (core.utc_iso(NOW + HOUR), "skip"))

    def test_without_prices_the_row_stays_untimed(self):
        row = rows(make_app(self.states()))["maint:washer:drum"]
        self.assertEqual(row["detail"], "Bomuld 90 + drain filter, door seal, due, last unknown")
        self.assertNotIn("at", row)
        self.assertNotIn("next", row)


DAY = 24 * HOUR


def state_file(app):
    return json.loads(Path(app.state_file).read_text())


def done_log(app):
    path = Path(app.done_log_file)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def after(count, name="dishwasher", first=NOW, make=None):
    """`count` more runs of `name` finishing one every two days from a day after `first`."""
    make = make or RUN_MAKERS[name][0]
    return [make(first + DAY + 2 * DAY * i) for i in range(count)]


def grow(app, name, count, first=NOW):
    """Appends `count` runs finishing after `first` to the ledger of `name`, the way a monitor does."""
    path = Path(app.state_dir) / f"{name}_feedback.json"
    cycles = json.loads(path.read_text())["cycles"]
    write_ledger(app.state_dir, name, cycles + after(count, name, first))


class RunTaskTests(unittest.TestCase):
    def test_the_tapped_tasks_count_from_the_moment_they_are_first_seen_and_the_others_from_the_ledger(self):
        shown = rows(make_app())
        tasks = ("maint:dishwasher:hygiene", "maint:dishwasher:filters", "maint:dishwasher:check", "maint:washer:drum",
                 "maint:washer:inlet", "maint:dryer:plinth")
        self.assertEqual({key: shown[key]["detail"] for key in tasks}, {
            "maint:dishwasher:hygiene": "Intensive 75, due, last unknown",
            "maint:dishwasher:filters": "rinse filter + arms, 30 runs left, ~13.12",
            "maint:dishwasher:check": "seal, sides, inside, 60 runs left, ~11.02.27",
            "maint:washer:drum": "Bomuld 90 + drain filter, door seal, due, last unknown",
            "maint:washer:inlet": "hose + valve filters, 90 runs left, ~12.04.27",
            "maint:dryer:plinth": "0 runs since cleaned, counting from 14.10",
        })

    def test_every_new_row_is_admin_only_with_its_room_and_its_guide(self):
        shown = rows(make_app())
        for key, area in (("dishwasher:hygiene", "kitchen"), ("dishwasher:filters", "kitchen"),
                          ("dishwasher:check", "kitchen"), ("washer:drum", "guest_bathroom"),
                          ("washer:inlet", "guest_bathroom"), ("dryer:plinth", "guest_bathroom")):
            row = shown[f"maint:{key}"]
            self.assertEqual((row["kind"], row["audience"], row["area"]), ("maint", "admin", area), key)
            self.assertEqual(row["steps"], list(guides.GUIDES[key].steps), key)
            self.assertTrue(core.is_valid_row(row), key)
        for key in ("dishwasher:filters", "dishwasher:check", "washer:inlet", "dryer:plinth"):
            self.assertEqual(shown[f"maint:{key}"]["done"], "tap", key)
        for key in ("dishwasher:hygiene", "washer:drum"):
            self.assertNotIn("done", shown[f"maint:{key}"], key)

    def test_the_rober2_rows_carry_their_guides_too(self):
        shown = rows(make_app())
        for part in ("filter", "main_brush", "side_brush", "sensor"):
            self.assertEqual(shown[f"maint:rober2:{part}"]["steps"], list(guides.GUIDES[f"rober2:{part}"].steps))

    def test_the_dryers_plinth_filter_is_low_key(self):
        row = rows(make_app())["maint:dryer:plinth"]
        self.assertNotIn("at", row)
        self.assertEqual(row["next"], "skip")
        self.assertEqual(row["info"], ["No interval yet. Log each clean to learn it.", "Counting from 14.10"])

    def test_the_seed_is_saved_at_once_and_never_moves(self):
        app = make_app()
        seeded = state_file(app)["done"]
        self.assertEqual(set(seeded), {"dishwasher:filters", "dishwasher:check", "washer:inlet", "dryer:plinth"})
        self.assertEqual(seeded["dishwasher:filters"], {"at": core.utc_iso(NOW), "runs": 50, "seed": True})
        self.assertEqual(seeded["washer:inlet"]["runs"], 60)
        tick(app, 3 * DAY)
        self.assertEqual(state_file(app)["done"], seeded)
        reloaded = make_app(app.states, state_dir=app.state_dir, now=app.now)
        self.assertEqual(reloaded._anchors, seeded)
        self.assertEqual(rows(reloaded)["maint:dishwasher:filters"]["info"][1], "Counting from 14.10")

    def test_a_run_that_has_finished_is_counted_by_the_next_tick(self):
        app = make_app()
        grow(app, "dishwasher", 2)
        tick(app, 6 * DAY)
        shown = rows(app)
        self.assertTrue(shown["maint:dishwasher:filters"]["detail"].startswith("rinse filter + arms, 28 runs left"))
        self.assertTrue(shown["maint:dishwasher:check"]["detail"].startswith("seal, sides, inside, 58 runs left"))
        self.assertTrue(shown["maint:washer:inlet"]["detail"].startswith("hose + valve filters, 90 runs left"))
        self.assertEqual(shown["maint:dishwasher:hygiene"]["detail"], "Intensive 75, due, last unknown")

    def test_a_run_going_when_the_tasks_were_seeded_is_counted_when_it_finishes(self):
        app = make_app()
        seeded_at = state_file(app)["done"]["dishwasher:filters"]["at"]
        cycles = every_two_days("dishwasher", NOW) + [dish_run(NOW + 2 * HOUR, duration_min=240.0)]
        write_ledger(app.state_dir, "dishwasher", cycles)
        tick(app, 3 * HOUR)
        self.assertEqual(state_file(app)["done"]["dishwasher:filters"]["at"], seeded_at)
        shown = rows(app)
        self.assertTrue(shown["maint:dishwasher:filters"]["detail"].startswith("rinse filter + arms, 29 runs left"))
        self.assertTrue(shown["maint:dishwasher:check"]["detail"].startswith("seal, sides, inside, 59 runs left"))
        self.assertTrue(shown["maint:washer:inlet"]["detail"].startswith("hose + valve filters, 90 runs left"))

    def test_a_ledger_is_read_again_only_when_its_file_has_changed(self):
        app = make_app()
        reads = []
        inner = hu.run_ledger.ledger
        hu.run_ledger.ledger = lambda cycles: reads.append(len(cycles or [])) or inner(cycles)
        try:
            for _ in range(3):
                tick(app)
            self.assertEqual(reads, [])
            grow(app, "dishwasher", 1)
            tick(app)
            tick(app)
            self.assertEqual(reads, [51])
        finally:
            hu.run_ledger.ledger = inner

    def test_an_unreadable_ledger_keeps_the_last_one_and_is_logged_once(self):
        app = make_app()
        path = Path(app.state_dir) / "dishwasher_feedback.json"
        path.write_text('{"cycles": [')
        os.utime(path, (1_800_000_000, 1_800_000_000))
        for _ in range(3):
            tick(app)
        self.assertIn("maint:dishwasher:filters", rows(app))
        bad = [msg for level, msg in app.logs if level == "WARNING" and "dishwasher_feedback.json" in msg]
        self.assertEqual(len(bad), 1)
        grow_ledger = every_two_days("dishwasher", NOW) + after(3)
        write_ledger(app.state_dir, "dishwasher", grow_ledger)
        tick(app, 8 * DAY)
        self.assertTrue(rows(app)["maint:dishwasher:filters"]["detail"].startswith("rinse filter + arms, 27 runs left"))

    def test_a_ledger_that_is_not_there_yet_gives_no_rows_and_is_seeded_when_it_appears(self):
        app = make_app(ledgers={"dryer": None})
        self.assertNotIn("maint:dryer:plinth", rows(app))
        self.assertNotIn("dryer:plinth", state_file(app)["done"])
        write_ledger(app.state_dir, "dryer", every_two_days("dryer", NOW))
        tick(app, 3 * DAY)
        self.assertEqual(rows(app)["maint:dryer:plinth"]["detail"], "0 runs since cleaned, counting from 17.10")
        self.assertEqual(state_file(app)["done"]["dryer:plinth"]["at"], core.utc_iso(NOW + 3 * DAY))
        self.assertEqual(state_file(app)["done"]["dishwasher:filters"]["at"], core.utc_iso(NOW))

    def test_no_ledger_at_all_leaves_only_the_other_rows(self):
        app = make_app(ledgers={"washer": None, "dishwasher": None, "dryer": None})
        self.assertEqual({key for key in rows(app) if key.startswith(("maint:dish", "maint:washer", "maint:dryer"))},
                         set())
        self.assertIn("maint:rober2:filter", rows(app))
        self.assertEqual(state_file(app).get("done", {}), {})

    def test_the_old_attribute_the_washer_published_is_no_longer_read(self):
        states = base_states()
        states["sensor.washer_state"] = full("Off", {"last_drum_clean": iso(NOW - 3 * DAY)})
        row = rows(make_app(states))["maint:washer:drum"]
        self.assertEqual(row["detail"], "Bomuld 90 + drain filter, door seal, due, last unknown")

    def test_an_old_state_file_with_the_drum_key_loads_and_loses_it(self):
        app = make_app()
        tick(app)
        data = state_file(app)
        data["drum"] = "2026-09-12T11:21:30+00:00"
        Path(app.state_file).write_text(json.dumps(data))
        reloaded = make_app(app.states, state_dir=app.state_dir, now=app.now)
        tick(reloaded)
        self.assertNotIn("drum", state_file(reloaded))
        self.assertEqual(reloaded._anchors, app._anchors)

    def test_the_dryers_interval_in_the_config_makes_its_row_a_normal_one(self):
        shown = rows(make_app(dryer={"interval_runs": 40}))
        row = shown["maint:dryer:plinth"]
        self.assertEqual(row["detail"], "plinth filter + air path, 40 runs left, ~02.01.27")
        self.assertEqual(row["at"], "2027-01-02T08:00:00+00:00")
        self.assertNotIn("next", row)
        self.assertEqual(row["info"][0], "Every 40 runs (~3 months)")

    def test_a_config_interval_that_is_not_a_whole_number_leaves_it_a_log(self):
        for bad in ("40", 0, -2, None, True, "soon"):
            row = rows(make_app(dryer={"interval_runs": bad}))["maint:dryer:plinth"]
            self.assertEqual(row["next"], "skip", bad)
            self.assertNotIn("at", row, bad)

    def test_the_shipped_config_leaves_the_interval_unset_and_the_dryer_in_the_guest_bathroom(self):
        self.assertEqual(CONFIG["dryer"], {"interval_runs": None})
        app = make_app()
        self.assertEqual(app.run_areas, {"washer": "guest_bathroom", "dishwasher": "kitchen", "dryer": "guest_bathroom",
                                         "coffee": "kitchen"})
        self.assertEqual(set(app.feedback_files), {"washer", "dishwasher", "dryer", "coffee"})

    def test_the_ledgers_default_to_the_files_next_to_the_appliance_modules(self):
        app = hu.HouseUpcoming.__new__(hu.HouseUpcoming)
        here = Path(hu.__file__).resolve().parent
        app.args = {**CONFIG, "state_file": f"{tempfile.mkdtemp()}/state.json"}
        app.get_state = lambda *a, **kw: None
        app.set_state = lambda *a, **kw: None
        app.get_now = lambda: NOW
        app.get_app = lambda name: None
        app.log = lambda *a, **kw: None
        for hook in ("listen_state", "listen_event", "run_every", "run_daily", "run_in"):
            setattr(app, hook, lambda *a, **kw: None)
        app.create_task = lambda coro: None
        app.initialize()
        self.assertEqual(app.feedback_files["washer"], here.parent / "appliances" / "washer_feedback.json")
        self.assertEqual(app.feedback_files["dishwasher"], here.parent / "appliances" / "dishwasher_feedback.json")
        self.assertEqual(app.feedback_files["dryer"], here.parent / "appliances" / "dryer_feedback.json")
        self.assertEqual(app.feedback_files["coffee"], here.parent / "coffee" / "coffee_feedback.json")
        self.assertEqual(app.done_log_file, here / "house_upcoming_done_log.jsonl")


def coffee_brews(count, last_end=NOW):
    return [{"id": f"c{int((last_end - (count - i) * DAY).timestamp())}", "ts": iso(last_end - (count - i) * DAY),
             "duration_min": 4.0, "completion_class": "complete", "outcome": "complete"} for i in range(count)]


class CoffeeRowTests(unittest.TestCase):
    """The coffee app's ledger gives the xBloom's four rows, like a monitor's feedback file."""

    def test_no_ledger_no_coffee_rows(self):
        self.assertFalse([key for key in rows(make_app()) if key.startswith("maint:coffee:")])

    def test_four_admin_kitchen_rows_in_brews_seeded_at_first_sight(self):
        app = make_app(ledgers={"coffee": coffee_brews(40)})
        shown = {key: row for key, row in rows(app).items() if key.startswith("maint:coffee:")}
        self.assertEqual(set(shown), {"maint:coffee:rinse", "maint:coffee:tank", "maint:coffee:burrs",
                                      "maint:coffee:descale"})
        for key, row in shown.items():
            self.assertEqual((row["audience"], row["area"], row["done"], row["icon"]),
                             ("admin", "kitchen", "tap", "mdi:coffee-maker"), key)
            self.assertIn("brews left", row["detail"], key)
            self.assertEqual(row["steps"], list(guides.GUIDES[key.removeprefix("maint:")].steps))
        self.assertEqual(shown["maint:coffee:rinse"]["info"][1], "Counting from 14.10")
        saved = state_file(app)["done"]
        self.assertEqual(saved["coffee:rinse"], {"at": core.utc_iso(NOW), "runs": 40, "seed": True})

    def test_an_empty_ledger_seeds_them_too(self):
        app = make_app(ledgers={"coffee": []})
        self.assertIn("maint:coffee:descale", rows(app))
        self.assertTrue(rows(app)["maint:coffee:descale"]["detail"].startswith("descaler, 2 rounds, 60 brews left"))

    def test_done_on_a_coffee_row(self):
        app = make_app(ledgers={"coffee": coffee_brews(10)})
        app._on_done("house_upcoming_done", {"row": "maint:coffee:tank"}, {})
        self.assertEqual(rows(app)["maint:coffee:tank"]["info"][1], "Done 14.10, no brews since")
        self.assertEqual(done_log(app)[-1]["row"], "maint:coffee:tank")


class DoneEventTests(unittest.TestCase):
    FILTERS = "maint:dishwasher:filters"

    def done(self, app, row=FILTERS):
        app._on_done("house_upcoming_done", {"row": row}, {})

    def undo(self, app, row=FILTERS):
        app._on_undo("house_upcoming_undo", {"row": row}, {})

    def test_the_events_are_listened_for(self):
        app = make_app()
        self.assertIn(("_on_done", "house_upcoming_done"), app.events)
        self.assertIn(("_on_undo", "house_upcoming_undo"), app.events)

    def test_done_starts_the_count_over_publishes_at_once_and_is_saved(self):
        app = make_app()
        grow(app, "dishwasher", 2)
        tick(app, 6 * DAY)
        self.assertTrue(rows(app)[self.FILTERS]["detail"].startswith("rinse filter + arms, 28 runs left"))
        seed = state_file(app)["done"]["dishwasher:filters"]
        published = len(app.published)
        self.done(app)
        self.assertEqual(len(app.published), published + 1)
        row = rows(app)[self.FILTERS]
        self.assertTrue(row["detail"].startswith("rinse filter + arms, 30 runs left"), row["detail"])
        self.assertEqual(row["info"][1], "Done 20.10, no runs since")
        saved = state_file(app)["done"]["dishwasher:filters"]
        self.assertEqual(saved, {"at": core.utc_iso(app.now), "runs": 52, "prev": seed})
        self.assertEqual(done_log(app), [{"event": "done", "row": self.FILTERS, "at": core.utc_iso(app.now), "runs": 52}])
        self.assertIn(("INFO", "Done: Dishwasher filters + spray arms, at run 52"), app.logs)

    def test_only_the_task_that_was_tapped_starts_over(self):
        app = make_app()
        grow(app, "dishwasher", 2)
        tick(app, 6 * DAY)
        self.done(app)
        shown = rows(app)
        self.assertTrue(shown["maint:dishwasher:check"]["detail"].startswith("seal, sides, inside, 58 runs left"))
        self.assertEqual(shown["maint:dishwasher:check"]["info"][1], "Counting from 14.10")

    def test_a_done_survives_a_reload(self):
        app = make_app()
        tick(app, DAY)
        self.done(app)
        reloaded = make_app(app.states, state_dir=app.state_dir, now=app.now)
        self.assertEqual(reloaded._anchors, app._anchors)
        self.assertEqual(rows(reloaded)[self.FILTERS], rows(app)[self.FILTERS])

    def test_undo_brings_the_seed_back_publishes_and_is_saved(self):
        app = make_app()
        tick(app, 2 * DAY)
        before = state_file(app)["done"]
        shown_before = rows(app)[self.FILTERS]
        self.done(app)
        published = len(app.published)
        self.undo(app)
        self.assertEqual(len(app.published), published + 1)
        self.assertEqual(rows(app)[self.FILTERS], shown_before)
        self.assertEqual(state_file(app)["done"], before)
        self.assertEqual([entry["event"] for entry in done_log(app)], ["done", "undo"])
        self.assertIn(("INFO", "Done undone: Dishwasher filters + spray arms"), app.logs)

    def test_a_second_undo_has_nothing_to_undo(self):
        app = make_app()
        self.done(app)
        self.undo(app)
        published, logged = len(app.published), done_log(app)
        self.undo(app)
        self.assertEqual((len(app.published), done_log(app)), (published, logged))
        self.assertTrue(any(level == "WARNING" and msg.startswith("Undo ignored") for level, msg in app.logs))

    def test_undoing_the_second_of_two_dones_brings_back_the_first(self):
        app = make_app()
        tick(app, DAY)
        self.done(app)
        first = state_file(app)["done"]["dishwasher:filters"]
        tick(app, DAY)
        self.done(app)
        self.undo(app)
        self.assertEqual({k: v for k, v in state_file(app)["done"]["dishwasher:filters"].items() if k != "prev"},
                         {k: v for k, v in first.items() if k != "prev"})

    def test_a_done_for_any_other_row_is_ignored_and_logged(self):
        app = make_app()
        before, published = state_file(app), len(app.published)
        for row in ("maint:washer:drum", "maint:dishwasher:hygiene", "maint:rober2:filter", "maint:nothing", "", None, 5):
            self.done(app, row)
        app._on_done("house_upcoming_done", {}, {})
        app._on_done("house_upcoming_done", None, {})
        self.assertEqual((state_file(app)["done"], len(app.published), done_log(app)), (before["done"], published, []))
        warned = [msg for level, msg in app.logs if level == "WARNING" and msg.startswith("Done ignored")]
        self.assertEqual(len(warned), 9)

    def test_a_done_without_runs_on_record_is_ignored(self):
        app = make_app(ledgers={"washer": None})
        self.done(app, "maint:washer:inlet")
        self.assertEqual(done_log(app), [])
        self.assertNotIn("maint:washer:inlet", rows(app))

    def test_the_dryers_plinth_filter_is_logged_with_the_same_event(self):
        app = make_app()
        grow(app, "dryer", 3)
        tick(app, 8 * DAY)
        self.assertEqual(rows(app)["maint:dryer:plinth"]["detail"], "3 runs since cleaned, counting from 14.10")
        self.done(app, "maint:dryer:plinth")
        row = rows(app)["maint:dryer:plinth"]
        self.assertEqual((row["detail"], row["info"][1]), ("0 runs since cleaned, last 22.10", "Logged 22.10, no runs since"))
        self.assertEqual(done_log(app)[0]["runs"], 53)

    def test_done_and_undo_leave_the_state_file_exactly_as_it_was(self):
        app = make_app()
        tick(app, 2 * DAY)
        before = Path(app.state_file).read_text()
        for row in ("maint:dishwasher:filters", "maint:dishwasher:check", "maint:washer:inlet", "maint:dryer:plinth"):
            self.done(app, row)
            self.undo(app, row)
        self.assertEqual(Path(app.state_file).read_text(), before)

    def test_a_failed_log_write_does_not_stop_the_done(self):
        app = make_app()
        app.done_log_file = Path(app.state_dir) / "no" / "such" / "dir" / "log.jsonl"
        self.done(app)
        self.assertTrue(rows(app)[self.FILTERS]["info"][1].startswith("Done "))
        self.assertTrue(any(level == "WARNING" and "Done log write failed" in msg for level, msg in app.logs))


class PushRunTests(unittest.TestCase):
    def push(self, app):
        app._on_push_time({})
        drain(app)

    def test_a_tapped_task_that_comes_due_is_pushed_once_and_again_only_after_a_done(self):
        app = make_app()
        grow(app, "dishwasher", 31)
        tick(app, 64 * DAY)
        self.assertEqual(rows(app)["maint:dishwasher:filters"]["detail"], "rinse filter + arms, due")
        self.push(app)
        sent = app.notifier.sent
        self.assertEqual([(s["title"], s["message"]) for s in sent], [("Maintenance due", "Dishwasher filters + spray arms")])
        tick(app, DAY)
        self.push(app)
        self.assertEqual(len(sent), 1)
        app._on_done("house_upcoming_done", {"row": "maint:dishwasher:filters"}, {})
        grow(app, "dishwasher", 31, first=app.now)
        tick(app, 64 * DAY)
        self.push(app)
        self.assertEqual([s["message"] for s in sent],
                         ["Dishwasher filters + spray arms", "Dishwasher filters + spray arms, Dishwasher general check"])

    def test_a_confirmed_task_nobody_ever_confirmed_is_due_but_never_pushed(self):
        app = make_app()
        shown = rows(app)
        self.assertIn("due", shown["maint:dishwasher:hygiene"]["detail"])
        self.assertIn("due", shown["maint:washer:drum"]["detail"])
        tick(app, 2 * DAY)
        self.push(app)
        self.assertEqual(app.notifier.sent, [])

    def test_a_confirmed_task_is_pushed_once_its_runs_since_the_last_confirmation_are_fifteen(self):
        cycles = every_two_days("dishwasher", NOW)
        cycles[-16] = dish_run(NOW - 31 * DAY, confirmed="intensive", programme_confirmed_by_human=True)
        app = make_app(ledgers={"dishwasher": cycles})
        self.assertEqual(rows(app)["maint:dishwasher:hygiene"]["detail"], "Intensive 75, due, last 13.09")
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Dishwasher hygiene run"])
        self.push(app)
        self.assertEqual(len(app.notifier.sent), 1)

    def test_the_drum_clean_is_pushed_fifteen_runs_after_the_last_confirmed_one(self):
        cycles = every_two_days("washer", NOW)
        cycles[-21] = drum_clean_run(NOW - 40 * DAY)
        app = make_app(ledgers={"washer": cycles})
        self.assertEqual(rows(app)["maint:washer:drum"]["detail"], "Bomuld 90 + drain filter, door seal, due, last 04.09")
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Washer drum + drawer clean"])

    def test_the_dryers_log_row_is_never_pushed(self):
        app = make_app()
        grow(app, "dryer", 200)
        tick(app, 400 * DAY)
        self.assertIn("runs since cleaned", rows(app)["maint:dryer:plinth"]["detail"])
        self.push(app)
        self.assertNotIn("Dryer plinth filter", " ".join(s["message"] for s in app.notifier.sent))

    def test_with_an_interval_the_plinth_filter_is_pushed_when_due(self):
        app = make_app(dryer={"interval_runs": 40})
        grow(app, "dryer", 41)
        tick(app, 84 * DAY)
        self.assertEqual(rows(app)["maint:dryer:plinth"]["detail"], "plinth filter + air path, due")
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Dryer plinth filter"])


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


class ReplaceByTests(unittest.TestCase):
    def test_the_roof_station_steps_give_its_replace_by_day(self):
        states = base_states()
        states["sensor.ecowitt_battery_8351"] = full("20", changed=datetime(2026, 10, 9, tzinfo=timezone.utc))
        app = make_app(states)
        app.history["sensor.ecowitt_battery_8351"] = [
            {"state": "60", "last_changed": "2026-08-19T12:00:00+00:00"},
            {"state": "40", "last_changed": "2026-09-01T04:00:00+00:00"},
            {"state": "20", "last_changed": "2026-10-09T00:00:00+00:00"},
        ]
        asyncio.run(app._history_job({}))
        drain(app)
        row = rows(app)["maint:battery:sensor.ecowitt_battery_8351"]
        self.assertEqual((row["title"], row["detail"]),
                         ("Roof weather station batteries", "replace by 16.11 - 33 days, 2x AA"))
        self.assertEqual(row["at"], "2026-11-16T08:00:00+00:00")
        self.assertEqual(app._facts["sensor.ecowitt_battery_8351"]["basis"], "trend")

    def test_the_model_median_pools_every_device_of_the_model(self):
        app = make_app()
        app._facts = {
            "sensor.hallway_presence_battery": {"in_since": "2026-09-11", "lifetimes": [148, 127, 204]},
            "sensor.kristines_room_presence_battery": {"in_since": "2026-06-02", "lifetimes": [79, 98]},
            "sensor.guest_bathroom_presence_battery": {"in_since": "2026-09-15", "lifetimes": [335]},
            "sensor.yale_battery": {"in_since": "2026-02-22"},
            "sensor.dining_room_window_1_battery": {"in_since": "2026-10-14", "lifetimes": [270],
                                                    "replace_by": "2026-10-01", "basis": "own"},
        }
        app._estimate_replace_by(date(2026, 10, 14))
        due = {entity: (facts.get("replace_by"), facts.get("basis")) for entity, facts in app._facts.items()}
        self.assertEqual(due, {
            "sensor.hallway_presence_battery": ("2027-02-06", "own"),
            "sensor.kristines_room_presence_battery": (None, None),
            "sensor.guest_bathroom_presence_battery": ("2027-01-30", "model"),
            "sensor.yale_battery": (None, None),
            "sensor.dining_room_window_1_battery": ("2027-07-11", "model"),
        })
        tick(app)
        shown = rows(app)
        self.assertEqual(shown["maint:battery:sensor.guest_bathroom_presence_battery"]["detail"],
                         "recharge by ~30.01.27 - 108 days, LADDA AAA NiMH")
        self.assertNotIn("maint:battery:sensor.kristines_room_presence_battery", shown)
        self.assertNotIn("maint:battery:sensor.yale_battery", shown)


class Rober2EstimateTests(unittest.TestCase):
    """Each part dated before it is due, by the cleaning rate the history job reads from the recorder."""

    CLEANING = "sensor.rober2_total_cleaning_time"
    LEFT = {"filter": 534003, "main_brush": 514101, "side_brush": 714003, "sensor": 102003}

    def states(self):
        states = base_states()
        for part, seconds in self.LEFT.items():
            states[f"sensor.rober2_{part}_time_left"] = full(str(seconds))
        return states

    def read_month(self, app, first="1041807", last_count="1131245"):
        app.history[self.CLEANING] = [{"state": first, "last_changed": iso(NOW - 30 * 24 * HOUR)},
                                      {"state": last_count, "last_changed": iso(NOW - HOUR)}]
        asyncio.run(app._history_job({}))
        drain(app)

    def sensor_detail(self, app):
        return rows(app)["maint:rober2:sensor"]["detail"]

    def test_every_part_is_dated_by_the_default_rate_until_the_history_job_has_run(self):
        app = make_app(self.states())
        self.assertEqual({part: rows(app)[f"maint:rober2:{part}"]["detail"] for part in self.LEFT}, {
            "filter": "replace by ~16.04.27 - 184 days", "main_brush": "replace by ~09.04.27 - 177 days",
            "side_brush": "replace by ~17.06.27 - 246 days", "sensor": "clean by ~18.11 - 35 days"})
        row = rows(app)["maint:rober2:sensor"]
        self.assertEqual((row["title"], row["at"], row["icon"], row["area"], row["audience"]),
                         ("Rober2 sensors", "2026-11-18T08:00:00+00:00", "mdi:robot-vacuum", "kitchen", "admin"))
        self.assertEqual(app.rober2_cleaning, CONFIG["rober2"]["cleaning_time"])

    def test_the_history_job_reads_the_last_thirty_days_and_the_rate_dates_the_rows(self):
        app = make_app(self.states())
        asked = []
        inner = app.get_history

        def get_history(entity_id=None, **kw):
            asked.append((entity_id, kw["start_time"], kw["end_time"]))
            return inner(entity_id=entity_id, **kw)

        app.get_history = get_history
        self.read_month(app)
        self.assertIn((self.CLEANING, NOW - 30 * 24 * HOUR, NOW), asked)
        self.assertAlmostEqual(app._rober2_rate, 89438 / 30)
        self.assertEqual(self.sensor_detail(app), "clean by ~17.11 - 34 days")
        self.assertIn(("INFO", "Rober2 cleaning rate 2981 s a day"), app.logs)

    def test_a_month_without_cleaning_goes_back_to_the_default_rate(self):
        app = make_app(self.states())
        self.read_month(app)
        self.read_month(app, first="1131245", last_count="1131245")
        self.assertEqual(app._rober2_rate, maint.ROBER2_RATE_DEFAULT)
        self.assertEqual(self.sensor_detail(app), "clean by ~18.11 - 35 days")

    def test_a_failed_read_keeps_the_rate_in_use(self):
        app = make_app(self.states())
        self.read_month(app)
        inner = app.get_history

        def get_history(entity_id=None, **kw):
            if entity_id == self.CLEANING:
                raise RuntimeError("recorder down")
            return inner(entity_id=entity_id, **kw)

        app.get_history = get_history
        asyncio.run(app._history_job({}))
        drain(app)
        self.assertAlmostEqual(app._rober2_rate, 89438 / 30)
        self.assertEqual(self.sensor_detail(app), "clean by ~17.11 - 34 days")
        self.assertTrue(any(level == "WARNING" and f"History of {self.CLEANING} failed" in msg
                            for level, msg in app.logs))

    def test_the_rate_survives_a_reload_and_a_bad_saved_one_is_the_default(self):
        app = make_app(self.states())
        self.read_month(app)
        tick(app)
        state_dir = str(Path(app.state_file).parent)
        reloaded = make_app(self.states(), state_dir=state_dir, now=app.now)
        self.assertAlmostEqual(reloaded._rober2_rate, 89438 / 30)
        self.assertEqual(last(reloaded)["attributes"]["rows"], last(app)["attributes"]["rows"])
        for bad in ("fast", 0, -3.5, None):
            Path(state_dir, "state.json").write_text(json.dumps({"version": hu.STATE_VERSION, "rober2_rate": bad}))
            self.assertEqual(make_app(self.states(), state_dir=state_dir)._rober2_rate, maint.ROBER2_RATE_DEFAULT)

    def test_a_part_at_zero_takes_its_row_over_and_a_reset_hands_it_back(self):
        app = make_app(self.states())
        self.assertIn("at", rows(app)["maint:rober2:sensor"])
        fire(app, "sensor.rober2_sensor_time_left", full("-5"))
        due = rows(app)["maint:rober2:sensor"]
        self.assertEqual(due["detail"], "clean")
        self.assertNotIn("at", due)
        self.assertEqual([row["id"] for row in last(app)["attributes"]["rows"]].count("maint:rober2:sensor"), 1)
        fire(app, "sensor.rober2_sensor_time_left", full("108000"))
        tick(app, 3 * HOUR)
        self.assertEqual(self.sensor_detail(app), "clean by ~20.11 - 37 days")

    def test_estimates_are_never_pushed(self):
        app = make_app(self.states())
        self.assertEqual([key for title, key in app._conditions if title.startswith("Rober2")], [None] * 4)
        app._on_push_time({})
        drain(app)
        self.assertEqual(app.notifier.sent, [])


class PlantTests(unittest.TestCase):
    """Houseplants: a soil moisture sensor opens the plant's watering row at or under its dry level and
    closes it five points above; the daily push names it once an episode. The behaviour tests run on
    SAMPLE_PLANT whatever the yaml lists, and the last one runs every plant the yaml lists."""

    ROW = "maint:plant:strelitzia"
    UNREADABLE = ("unavailable", "unknown", "", "n/a")

    @staticmethod
    def states(moisture="35"):
        states = base_states()
        states[MOISTURE] = full(moisture)
        return states

    def make(self, states=None, **args):
        return make_app(self.states() if states is None else states, **{"plants": [SAMPLE_PLANT], **args})

    def push(self, app):
        app._on_push_time({})
        drain(app)

    def test_the_sensor_is_a_listened_source_and_a_wet_plant_has_no_row(self):
        app = self.make()
        self.assertIn((MOISTURE, {"attribute": "all"}), app.listened)
        self.assertNotIn(self.ROW, rows(app))
        self.assertNotIn("Plant moisture sensors HA does not have", " ".join(msg for _, msg in app.logs))

    def test_a_dry_reading_opens_an_admin_row_in_the_plants_room(self):
        app = self.make()
        fire(app, MOISTURE, full("12"))
        row = rows(app)[self.ROW]
        self.assertEqual((row["kind"], row["title"], row["detail"], row["icon"], row["area"], row["audience"]),
                         ("maint", "Water the Strelitzia", "soil 12%, water at 15%", "mdi:watering-can-outline",
                          "dining_room", "admin"))
        self.assertEqual(set(row) & {"at", "next", "done", "info", "steps", "warn"}, set())
        self.assertTrue(core.is_valid_row(row))

    def test_the_row_closes_only_five_points_above_dry(self):
        app = self.make()
        for state, detail in (("15", "soil 15%"), ("16", "soil 16%"), ("19", "soil 19%"), ("12", "soil 12%"),
                              ("17", "soil 17%")):
            fire(app, MOISTURE, full(state))
            self.assertEqual(rows(app)[self.ROW]["detail"], f"{detail}, water at 15%", state)
        for state, shown in (("20", False), ("16", False), ("15", True), ("20", False), ("100", False)):
            fire(app, MOISTURE, full(state))
            self.assertEqual(self.ROW in rows(app), shown, state)

    def test_an_unreadable_reading_changes_nothing_and_has_no_row(self):
        app = self.make()
        for state in self.UNREADABLE:
            fire(app, MOISTURE, full(state))
            self.assertNotIn(self.ROW, rows(app), state)
            self.assertEqual(app._plant_latches[MOISTURE], {}, state)
        fire(app, MOISTURE, full("12"))
        latch = dict(app._plant_latches[MOISTURE])
        self.assertEqual(list(latch), ["since"])
        for state in self.UNREADABLE:
            fire(app, MOISTURE, full(state))
            self.assertNotIn(self.ROW, rows(app), state)
            self.assertEqual(app._plant_latches[MOISTURE], latch, state)
        fire(app, MOISTURE, full("17"))
        self.assertEqual(rows(app)[self.ROW]["detail"], "soil 17%, water at 15%")

    def test_the_latch_is_saved_with_the_rest_and_a_reload_does_not_push_again(self):
        app = self.make()
        fire(app, MOISTURE, full("12"))
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Water the Strelitzia"])
        saved = state_file(app)
        self.assertEqual(saved["plants"], {MOISTURE: {"since": core.utc_iso(NOW)}})
        self.assertIn(f"plant:{MOISTURE}:{core.utc_iso(NOW)}", saved["pushed"])
        reloaded = self.make(app.states, state_dir=app.state_dir, now=app.now)
        self.assertEqual(reloaded._plant_latches, app._plant_latches)
        self.assertEqual(rows(reloaded)[self.ROW], rows(app)[self.ROW])
        self.push(reloaded)
        self.assertEqual(reloaded.notifier.sent, [])

    def test_a_restart_that_finds_the_sensor_unavailable_keeps_the_latch_and_pushes_nothing_new(self):
        app = self.make()
        fire(app, MOISTURE, full("12"))
        self.push(app)
        tick(app, MIN)
        app.states[MOISTURE] = full("unavailable")
        reloaded = self.make(app.states, state_dir=app.state_dir, now=app.now)
        self.assertNotIn(self.ROW, rows(reloaded))
        fire(reloaded, MOISTURE, full("13"))
        self.assertEqual(rows(reloaded)[self.ROW]["detail"], "soil 13%, water at 15%")
        self.push(reloaded)
        self.assertEqual(reloaded.notifier.sent, [])

    def test_a_latch_that_was_open_before_a_restart_closes_on_the_first_wet_reading(self):
        app = self.make()
        fire(app, MOISTURE, full("12"))
        tick(app, MIN)
        app.states[MOISTURE] = full("40")
        reloaded = self.make(app.states, state_dir=app.state_dir, now=app.now)
        self.assertNotIn(self.ROW, rows(reloaded))
        self.assertEqual(reloaded._plant_latches, {MOISTURE: {}})

    def test_a_saved_latch_for_a_plant_no_longer_listed_is_dropped(self):
        app = self.make()
        fire(app, MOISTURE, full("12"))
        tick(app, MIN)
        self.assertIn("since", state_file(app)["plants"][MOISTURE])
        reloaded = self.make(app.states, state_dir=app.state_dir, now=app.now, plants=[])
        self.assertNotIn(self.ROW, rows(reloaded))
        self.assertEqual(reloaded._plant_latches, {})

    def test_pushed_once_an_episode_and_again_once_it_was_watered_and_dried_out(self):
        app = self.make()
        fire(app, MOISTURE, full("12"))
        for _ in range(3):
            self.push(app)
            tick(app, DAY)
        self.assertEqual(len(app.notifier.sent), 1)
        fire(app, MOISTURE, full("30"))
        tick(app, DAY)
        self.push(app)
        fire(app, MOISTURE, full("14"))
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Water the Strelitzia"] * 2)

    def test_the_amount_to_pour_is_in_the_title_and_the_push_but_not_the_detail(self):
        poured = {**SAMPLE_PLANT, "water": 3}
        app = self.make(self.states("12"), plants=[poured])
        row = rows(app)[self.ROW]
        self.assertEqual((row["title"], row["detail"]), ("Water the Strelitzia: 3 L", "soil 12%, water at 15%"))
        self.assertTrue(core.is_valid_row(row))
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Water the Strelitzia: 3 L"])

    def test_the_push_names_it_with_the_other_conditions(self):
        states = self.states("9")
        states["sensor.rober2_filter_time_left"] = full("-19902")
        app = self.make(states)
        self.push(app)
        self.assertEqual([(s["title"], s["message"]) for s in app.notifier.sent],
                         [("Maintenance due", "Rober2 filter, Water the Strelitzia")])

    def test_more_plants_are_more_lines_and_a_sensor_ha_does_not_have_is_named_at_start(self):
        fern = {"moisture": "sensor.fern_moisture", "name": "Boston fern", "area": "kitchen", "dry": 30}
        ficus = {"moisture": "sensor.ficus_moistur", "name": "Ficus", "dry": 20}
        states = self.states()
        states["sensor.fern_moisture"] = full("28")
        app = self.make(states, plants=[SAMPLE_PLANT, fern, ficus])
        shown = rows(app)
        self.assertEqual((shown["maint:plant:boston_fern"]["detail"], shown["maint:plant:boston_fern"]["area"]),
                         ("soil 28%, water at 30%", "kitchen"))
        self.assertNotIn(self.ROW, shown)
        self.assertNotIn("maint:plant:ficus", shown)
        self.assertIn(("sensor.fern_moisture", {"attribute": "all"}), app.listened)
        self.assertIn(("WARNING", "Plant moisture sensors HA does not have: sensor.ficus_moistur"), app.logs)
        fire(app, "sensor.fern_moisture", full("40"))
        self.assertNotIn("maint:plant:boston_fern", rows(app))

    def test_a_low_conductivity_adds_the_feed_to_the_title_and_the_push_while_it_lasts(self):
        ec = "sensor.strelitzia_conductivity"
        fed = {**SAMPLE_PLANT, "water": 3, "conductivity": ec}
        states = self.states("12")
        states[ec] = full("90")
        app = self.make(states, plants=[fed])
        self.assertIn((ec, {"attribute": "all"}), app.listened)
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Strelitzia: 3 L + 3 tsp fertiliser")
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Water the Strelitzia: 3 L + 3 tsp fertiliser"])
        plain, with_feed = "Water the Strelitzia: 3 L", "Water the Strelitzia: 3 L + 3 tsp fertiliser"
        steps = (("350", plain), ("349", with_feed), ("unavailable", plain), ("0", with_feed), ("807", plain))
        for state, title in steps:
            fire(app, ec, full(state))
            self.assertEqual(rows(app)[self.ROW]["title"], title, state)

    def test_a_conductivity_that_cannot_be_read_says_nothing_and_a_sensor_ha_does_not_have_is_named(self):
        fed = {**SAMPLE_PLANT, "water": 3, "conductivity": "sensor.ec_typo"}
        app = self.make(self.states("12"), plants=[fed])
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Strelitzia: 3 L")
        self.assertIn(("WARNING", "Plant conductivity sensors HA does not have: sensor.ec_typo"), app.logs)
        self.assertNotIn("Plant moisture sensors HA does not have", " ".join(msg for _, msg in app.logs))

    def test_a_plant_without_a_conductivity_sensor_never_says_feed(self):
        app = self.make(self.states("12"), plants=[{**SAMPLE_PLANT, "water": 3}])
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Strelitzia: 3 L")

    def test_a_typo_in_the_plants_fails_at_start(self):
        with self.assertRaises(ValueError):
            self.make(plants=[{**SAMPLE_PLANT, "dry": "wet"}])

    def test_every_plant_the_yaml_lists_is_listened_to_and_has_its_own_row_that_opens_dry_and_closes_above(self):
        self.assertIsInstance(CONFIG["plants"], list)
        self.assertEqual(len(PLANTS) + len(TIMED), len(CONFIG["plants"]))
        app = make_app(plants=CONFIG["plants"])
        self.assertEqual(plant_rows(app, PLANTS.values()), {})
        self.assertNotIn("Plant moisture sensors HA does not have", " ".join(msg for _, msg in app.logs))
        for entity, plant in PLANTS.items():
            self.assertIn((entity, {"attribute": "all"}), app.listened)
            self.assertIn(plant["area"], AREAS, entity)
            self.assertEqual(core.clean_text(plant["name"]), plant["name"], entity)
            dry = f"{plant['dry']:g}"
            title = f"Water the {plant['name']}" + (f": {plant['water']:g} L" if plant["water"] else "")
            fire(app, entity, full(dry))
            row = plant_rows(app, PLANTS.values())[f"maint:plant:{plant['slug']}"]
            self.assertEqual((row["title"], row["detail"], row.get("area")),
                             (title, f"soil {dry}%, water at {dry}%", plant["area"]), entity)
            self.assertEqual(row.get("info"), plant["info"] or None, entity)
            self.assertTrue(core.is_valid_row(row), entity)
            if plant["conductivity"]:
                self.assertIn((plant["conductivity"], {"attribute": "all"}), app.listened, entity)
                fire(app, plant["conductivity"], full("100"))
                fed = plant_rows(app, PLANTS.values())[f"maint:plant:{plant['slug']}"]["title"]
                self.assertEqual(fed, f"{title} + {plants.tsp_text(plant['water'])} tsp fertiliser", entity)
                fire(app, plant["conductivity"], full("800"))
                plain = plant_rows(app, PLANTS.values())[f"maint:plant:{plant['slug']}"]["title"]
                self.assertEqual(plain, title, entity)
        self.assertEqual(len(plant_rows(app, PLANTS.values())), len(PLANTS))
        for entity, plant in PLANTS.items():
            fire(app, entity, full(f"{plant['dry'] + plants.CLOSE_MARGIN:g}"))
        self.assertEqual(plant_rows(app, PLANTS.values()), {})


class TimedPlantTests(unittest.TestCase):
    """Houseplants watered by the calendar: a tapped task counted in days, seeded at first sight, without a
    row until the day before it is due, then dated, then untimed and pushed once; Done starts over and undo
    takes it back. The behaviour tests run on SAMPLE_TIMED whatever the yaml lists, and the last two run
    every plant it lists."""

    ROW = "maint:plant:monstera"
    KEY = "plant:monstera"
    NO_LEDGERS = {"washer": None, "dishwasher": None, "dryer": None}

    def make(self, **args):
        return make_app(**{"plants": [SAMPLE_TIMED], **args})

    def push(self, app):
        app._on_push_time({})
        drain(app)

    def done(self, app, row=ROW):
        app._on_done("house_upcoming_done", {"row": row}, {})

    def undo(self, app, row=ROW):
        app._on_undo("house_upcoming_undo", {"row": row}, {})

    def test_the_plant_is_seeded_at_first_sight_and_has_no_row_until_the_day_before_it_is_due(self):
        app = self.make()
        self.assertEqual(state_file(app)["done"][self.KEY], {"at": core.utc_iso(NOW), "seed": True})
        for _ in range(8):
            self.assertNotIn(self.ROW, rows(app))
            tick(app, DAY)
        self.assertNotIn(self.ROW, rows(app))
        tick(app, DAY)
        row = rows(app)[self.ROW]
        self.assertEqual((row["kind"], row["title"], row["detail"], row["icon"], row["area"], row["audience"]),
                         ("maint", "Water the Monstera: 1 L", "1 day left, 24.10", "mdi:watering-can-outline",
                          "kitchen", "admin"))
        self.assertEqual((row["at"], row["done"]), ("2026-10-24T07:00:00+00:00", "tap"))
        self.assertEqual(row["info"], ["Every 10 days", "Counting from 14.10"])
        self.assertTrue(core.is_valid_row(row))

    def test_the_seed_never_moves_and_survives_a_reload(self):
        app = self.make()
        seeded = state_file(app)["done"]
        tick(app, 9 * DAY)
        self.assertEqual(state_file(app)["done"], seeded)
        reloaded = self.make(state_dir=app.state_dir, now=app.now)
        self.assertEqual(reloaded._anchors, app._anchors)
        self.assertEqual(rows(reloaded)[self.ROW], rows(app)[self.ROW])

    def test_it_turns_due_on_the_day_untimed_and_is_pushed_once(self):
        app = self.make()
        tick(app, 9 * DAY)
        self.assertEqual(rows(app)[self.ROW]["detail"], "1 day left, 24.10")
        self.push(app)
        self.assertEqual(app.notifier.sent, [])
        tick(app, DAY)
        row = rows(app)[self.ROW]
        self.assertEqual(row["detail"], "due today")
        self.assertNotIn("at", row)
        self.push(app)
        self.assertEqual([(s["title"], s["message"]) for s in app.notifier.sent],
                         [("Maintenance due", "Water the Monstera: 1 L")])
        tick(app, 2 * DAY)
        self.assertEqual(rows(app)[self.ROW]["detail"], "due since 24.10")
        self.push(app)
        self.assertEqual(len(app.notifier.sent), 1)

    def test_done_starts_the_interval_over_publishes_at_once_logs_and_is_saved(self):
        app = self.make()
        tick(app, 11 * DAY)
        seed = state_file(app)["done"][self.KEY]
        published = len(app.published)
        self.done(app)
        self.assertEqual(len(app.published), published + 1)
        self.assertNotIn(self.ROW, rows(app))
        self.assertEqual(state_file(app)["done"][self.KEY],
                         {"at": core.utc_iso(app.now), "prev": seed, "fed_at": seed["at"]})
        self.assertEqual(done_log(app), [{"event": "done", "row": self.ROW, "at": core.utc_iso(app.now)}])
        self.assertIn(("INFO", "Done: water the Monstera"), app.logs)
        tick(app, 9 * DAY)
        row = rows(app)[self.ROW]
        self.assertEqual((row["detail"], row["at"]), ("1 day left, 04.11", "2026-11-04T08:00:00+00:00"))
        self.assertEqual(row["info"], ["Every 10 days", "Done 25.10, 9 days ago"])

    def test_done_needs_no_appliance_ledger(self):
        app = self.make(ledgers=self.NO_LEDGERS)
        self.assertNotIn("maint:washer:inlet", rows(app))
        tick(app, 11 * DAY)
        self.done(app)
        self.assertEqual(state_file(app)["done"][self.KEY]["at"], core.utc_iso(app.now))
        self.assertEqual(len(done_log(app)), 1)

    def test_a_done_survives_a_reload_and_is_pushed_again_in_the_next_episode(self):
        app = self.make()
        tick(app, 11 * DAY)
        self.push(app)
        self.done(app)
        reloaded = self.make(state_dir=app.state_dir, now=app.now)
        self.assertEqual(reloaded._anchors, app._anchors)
        self.assertNotIn(self.ROW, rows(reloaded))
        tick(reloaded, 5 * DAY)
        self.push(reloaded)
        self.assertEqual(reloaded.notifier.sent, [])
        tick(reloaded, 6 * DAY)
        self.push(reloaded)
        self.assertEqual([s["message"] for s in reloaded.notifier.sent], ["Water the Monstera: 1 L"])

    def test_undo_brings_the_seed_back_publishes_logs_and_a_second_undo_is_ignored(self):
        app = self.make()
        tick(app, 9 * DAY)
        before, shown = state_file(app)["done"], rows(app)[self.ROW]
        self.done(app)
        self.assertNotIn(self.ROW, rows(app))
        published = len(app.published)
        self.undo(app)
        self.assertEqual(len(app.published), published + 1)
        self.assertEqual(rows(app)[self.ROW], shown)
        self.assertEqual(state_file(app)["done"], before)
        self.assertEqual([entry["event"] for entry in done_log(app)], ["done", "undo"])
        self.assertEqual(set(done_log(app)[1]), {"event", "row", "at"})
        self.assertIn(("INFO", "Done undone: water the Monstera"), app.logs)
        logged, published = done_log(app), len(app.published)
        self.undo(app)
        self.assertEqual((done_log(app), len(app.published)), (logged, published))
        self.assertTrue(any(level == "WARNING" and msg.startswith("Undo ignored") for level, msg in app.logs))

    def test_a_done_before_the_row_shows_still_counts(self):
        app = self.make()
        tick(app, 3 * DAY)
        self.assertNotIn(self.ROW, rows(app))
        self.done(app)
        self.assertEqual(state_file(app)["done"][self.KEY]["at"], core.utc_iso(app.now))
        self.assertEqual(len(done_log(app)), 1)

    def test_in_winter_feed_comes_after_twenty_eight_days_and_a_done_records_it_and_undo_takes_it_back(self):
        app = self.make()
        tick(app, 9 * DAY)
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Monstera: 1 L")
        seed_time = state_file(app)["done"][self.KEY]["at"]
        tick(app, DAY)
        self.done(app)
        first = state_file(app)["done"][self.KEY]
        self.assertEqual(first["fed_at"], seed_time)
        self.assertEqual(done_log(app)[-1], {"event": "done", "row": self.ROW, "at": core.utc_iso(app.now)})
        tick(app, 9 * DAY)
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Monstera: 1 L")
        tick(app, DAY)
        self.done(app)
        self.assertEqual(state_file(app)["done"][self.KEY]["fed_at"], seed_time)
        tick(app, 9 * DAY)
        row = rows(app)[self.ROW]
        self.assertEqual((row["title"], row["detail"]),
                         ("Water the Monstera: 1 L + 1 tsp fertiliser", "1 day left, 13.11"))
        self.push(app)
        tick(app, DAY)
        self.push(app)
        self.assertEqual([s["message"] for s in app.notifier.sent], ["Water the Monstera: 1 L + 1 tsp fertiliser"])
        before = {k: v for k, v in state_file(app)["done"][self.KEY].items() if k != "prev"}
        self.done(app)
        fed = state_file(app)["done"][self.KEY]
        self.assertEqual((fed["fed_at"], fed["at"]), (core.utc_iso(app.now), core.utc_iso(app.now)))
        self.assertEqual(done_log(app)[-1],
                         {"event": "done", "row": self.ROW, "at": core.utc_iso(app.now), "fed": True})
        self.assertIn(("INFO", "Done: water the Monstera, with feed"), app.logs)
        tick(app, 9 * DAY)
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Monstera: 1 L")
        self.undo(app)
        self.assertEqual(state_file(app)["done"][self.KEY], before)
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Monstera: 1 L + 1 tsp fertiliser")

    def test_from_march_to_september_every_watering_comes_with_feed(self):
        june = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        app = self.make(now=june)
        tick(app, 9 * DAY)
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Monstera: 1 L + 1 tsp fertiliser")
        tick(app, DAY)
        self.done(app)
        self.assertEqual(state_file(app)["done"][self.KEY]["fed_at"], core.utc_iso(app.now))
        tick(app, 9 * DAY)
        self.assertEqual(rows(app)[self.ROW]["title"], "Water the Monstera: 1 L + 1 tsp fertiliser")

    def test_a_succulent_is_fed_only_in_the_warm_months_and_then_at_most_every_twenty_eight_days(self):
        cactus = {**SAMPLE_TIMED, "name": "Cactus", "water": 0.1, "every_days": 35, "feed": "summer"}
        winter = self.make(plants=[cactus])
        tick(winter, 34 * DAY)
        self.assertEqual(rows(winter)["maint:plant:cactus"]["title"], "Water the Cactus: 0.1 L")
        tick(winter, 40 * DAY)
        self.assertEqual(rows(winter)["maint:plant:cactus"]["title"], "Water the Cactus: 0.1 L")
        june = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        summer = self.make(plants=[cactus], now=june)
        tick(summer, 34 * DAY)
        self.assertEqual(rows(summer)["maint:plant:cactus"]["title"], "Water the Cactus: 0.1 L + 1/4 tsp fertiliser")
        soon = self.make(plants=[{**cactus, "every_days": 14}], now=june)
        tick(soon, 13 * DAY)
        self.assertEqual(rows(soon)["maint:plant:cactus"]["title"], "Water the Cactus: 0.1 L")

    def test_a_sensor_plants_row_and_unknown_ids_are_no_tapped_task(self):
        app = self.make(plants=[SAMPLE_TIMED, SAMPLE_PLANT])
        before, published = state_file(app), len(app.published)
        for row in ("maint:plant:strelitzia", "maint:plant:nothing", "maint:monstera", "plant:monstera", "", None, 5):
            self.done(app, row)
            self.undo(app, row)
        self.assertEqual((state_file(app)["done"], len(app.published), done_log(app)), (before["done"], published, []))

    def test_the_other_rows_are_left_alone_by_a_plants_done(self):
        app = self.make()
        tick(app, 11 * DAY)
        before = {key: row for key, row in rows(app).items() if key != self.ROW}
        self.done(app)
        self.assertEqual({key: row for key, row in rows(app).items() if key != self.ROW}, before)

    def test_every_plant_the_yaml_lists_for_the_calendar_is_seeded_now_and_shows_from_the_day_before_it_is_due(self):
        self.assertTrue(TIMED)
        app = make_app(plants=CONFIG["plants"])
        self.assertEqual(plant_rows(app, TIMED.values()), {})
        for plant in TIMED.values():
            self.assertEqual(state_file(app)["done"][f"plant:{plant['slug']}"], {"at": core.utc_iso(NOW), "seed": True})
        elapsed = 0
        for days in sorted({plant["every_days"] for plant in TIMED.values()}):
            tick(app, (days - 1 - elapsed) * DAY)
            elapsed = days - 1
            shown = plant_rows(app, TIMED.values())
            visible = [plant for plant in TIMED.values() if plant["every_days"] - 1 <= elapsed]
            self.assertEqual(set(shown), {f"maint:plant:{plant['slug']}" for plant in visible}, days)
            for plant in visible:
                row, left = shown[f"maint:plant:{plant['slug']}"], plant["every_days"] - elapsed
                due = date(2026, 10, 14) + timedelta(days=plant["every_days"])
                title = f"Water the {plant['name']}" + (f": {plant['water']:g} L" if plant["water"] else "")
                self.assertTrue(row["title"].startswith(title), (row["title"], title))
                fed = row["title"] != title
                self.assertEqual(fed, elapsed >= plants.FEED_EVERY_DAYS and plant["feed"] is None, plant["name"])
                if fed:
                    self.assertEqual(row["title"], f"{title} + {plants.tsp_text(plant['water'])} tsp fertiliser")
                self.assertEqual((row["kind"], row["audience"], row["done"], row.get("area")),
                                 ("maint", "admin", "tap", plant["area"]), plant["name"])
                nine = core.utc_iso(datetime.combine(due, maint.DUE_TIME, maint.HOUSE_TZ))
                self.assertEqual(row.get("at"), nine if left > 0 else None, plant["name"])
                notes = plant["info"]
                self.assertEqual(row["info"][:len(notes)], notes, plant["name"])
                self.assertEqual(row["info"][len(notes):][0], f"Every {plant['every_days']} days", plant["name"])
                self.assertEqual(len(row["info"]), len(notes) + 2, plant["name"])
                self.assertIn(plant["area"], AREAS, plant["name"])
                self.assertEqual(core.clean_text(plant["name"]), plant["name"])
                self.assertTrue(core.is_valid_row(row), plant["name"])

    def test_a_done_on_each_plant_the_yaml_lists_is_logged_and_takes_only_that_row_away(self):
        app = make_app(plants=CONFIG["plants"], ledgers=self.NO_LEDGERS)
        tick(app, 60 * DAY)
        self.assertEqual(len(plant_rows(app, TIMED.values())), len(TIMED))
        for count, plant in enumerate(TIMED.values(), 1):
            row_id = f"maint:plant:{plant['slug']}"
            self.done(app, row_id)
            self.assertNotIn(row_id, rows(app), plant["name"])
            self.assertEqual(len(done_log(app)), count)
            self.assertEqual(len(plant_rows(app, TIMED.values())), len(TIMED) - count)
        ids = [f"maint:plant:{plant['slug']}" for plant in TIMED.values()]
        self.assertEqual([entry["row"] for entry in done_log(app)], ids)


class OnceTaskTests(unittest.TestCase):
    """One-off tasks: a tapped task with a day on it, seeded at first sight, without a row until the day
    before, then dated, then untimed and pushed once; a Done takes it away for good and undo brings it
    back. The behaviour tests run on SAMPLE_ONCE whatever the yaml lists, and the last one runs every task
    it lists."""

    ROW = "maint:once:check_the_fern"
    KEY = "once:check_the_fern"

    def make(self, **args):
        return make_app(**{"once": [SAMPLE_ONCE], **args})

    def push(self, app):
        app._on_push_time({})
        drain(app)

    def done(self, app, row=ROW):
        app._on_done("house_upcoming_done", {"row": row}, {})

    def undo(self, app, row=ROW):
        app._on_undo("house_upcoming_undo", {"row": row}, {})

    def test_the_task_is_seeded_at_first_sight_and_has_no_row_until_the_day_before(self):
        app = self.make()
        self.assertEqual(state_file(app)["done"][self.KEY], {"at": core.utc_iso(NOW), "seed": True})
        tick(app, 8 * DAY)
        self.assertNotIn(self.ROW, rows(app))
        tick(app, DAY)
        row = rows(app)[self.ROW]
        self.assertEqual((row["kind"], row["title"], row["detail"], row["icon"], row["area"], row["audience"]),
                         ("maint", "Check the fern", "leaves clean?", "mdi:bug-outline", "bathroom", "admin"))
        self.assertEqual((row["at"], row["done"], row["info"]), ("2026-10-24T07:00:00+00:00", "tap", ["Due 24.10"]))
        self.assertTrue(core.is_valid_row(row))

    def test_on_the_day_it_is_untimed_and_pushed_once(self):
        app = self.make()
        tick(app, 9 * DAY)
        self.push(app)
        self.assertEqual(app.notifier.sent, [])
        tick(app, DAY)
        self.assertNotIn("at", rows(app)[self.ROW])
        self.push(app)
        self.assertEqual([(s["title"], s["message"]) for s in app.notifier.sent],
                         [("Maintenance due", "Check the fern")])
        tick(app, 3 * DAY)
        self.assertIn(self.ROW, rows(app))
        self.push(app)
        self.assertEqual(len(app.notifier.sent), 1)

    def test_done_takes_it_away_for_good_logs_and_survives_a_reload(self):
        app = self.make()
        tick(app, 10 * DAY)
        seed = state_file(app)["done"][self.KEY]
        published = len(app.published)
        self.done(app)
        self.assertEqual(len(app.published), published + 1)
        self.assertNotIn(self.ROW, rows(app))
        self.assertEqual(state_file(app)["done"][self.KEY], {"at": core.utc_iso(app.now), "prev": seed})
        self.assertEqual(done_log(app), [{"event": "done", "row": self.ROW, "at": core.utc_iso(app.now)}])
        self.assertIn(("INFO", "Done: Check the fern"), app.logs)
        reloaded = self.make(state_dir=app.state_dir, now=app.now)
        for _ in range(3):
            tick(reloaded, 20 * DAY)
            self.assertNotIn(self.ROW, rows(reloaded))
            self.push(reloaded)
        self.assertEqual(reloaded.notifier.sent, [])

    def test_a_second_done_is_ignored_so_one_undo_still_brings_it_back(self):
        app = self.make()
        tick(app, 10 * DAY)
        self.done(app)
        saved, logged = state_file(app)["done"], done_log(app)
        self.done(app)
        self.assertEqual((state_file(app)["done"], done_log(app)), (saved, logged))
        self.assertTrue(any(level == "WARNING" and msg.startswith("Done ignored") for level, msg in app.logs))
        self.undo(app)
        self.assertIn(self.ROW, rows(app))

    def test_undo_brings_the_row_and_the_seed_back_and_a_second_undo_is_ignored(self):
        app = self.make()
        tick(app, 10 * DAY)
        before, shown = state_file(app)["done"], rows(app)[self.ROW]
        self.done(app)
        published = len(app.published)
        self.undo(app)
        self.assertEqual(len(app.published), published + 1)
        self.assertEqual(rows(app)[self.ROW], shown)
        self.assertEqual(state_file(app)["done"], before)
        self.assertEqual([entry["event"] for entry in done_log(app)], ["done", "undo"])
        self.assertIn(("INFO", "Done undone: Check the fern"), app.logs)
        logged = done_log(app)
        self.undo(app)
        self.assertEqual(done_log(app), logged)
        self.assertTrue(any(level == "WARNING" and msg.startswith("Undo ignored") for level, msg in app.logs))

    def test_other_ids_are_no_one_off_task(self):
        app = self.make(plants=[SAMPLE_TIMED])
        before = state_file(app)["done"]
        for row in ("maint:once:nothing", "maint:plant:check_the_fern", "once:check_the_fern", "", None, 5):
            self.done(app, row)
            self.undo(app, row)
        self.assertEqual((state_file(app)["done"], done_log(app)), (before, []))

    def test_every_task_the_yaml_lists_is_seeded_now_and_shows_the_day_before_its_day_until_done(self):
        self.assertTrue(ONCE)
        app = make_app(once=CONFIG["once"])
        for task in ONCE.values():
            row_id = f"maint:once:{task['slug']}"
            self.assertEqual(state_file(app)["done"][f"once:{task['slug']}"], {"at": core.utc_iso(NOW), "seed": True})
            self.assertNotIn(row_id, rows(app))
            days = (task["due"] - date(2026, 10, 14)).days
            tick(app, (days - 2) * DAY)
            self.assertNotIn(row_id, rows(app), task["title"])
            tick(app, DAY)
            row = rows(app)[row_id]
            nine = core.utc_iso(datetime.combine(task["due"], maint.DUE_TIME, maint.HOUSE_TZ))
            self.assertEqual((row["title"], row["at"], row["done"], row["audience"], row["kind"]),
                             (task["title"], nine, "tap", "admin", "maint"), task["title"])
            self.assertEqual((row.get("detail"), row.get("area"), row["icon"]),
                             (task["detail"] or None, task["area"], task["icon"]), task["title"])
            self.assertIn(task["area"], AREAS, task["title"])
            self.assertTrue(core.is_valid_row(row), task["title"])
            self.done(app, row_id)
            self.assertNotIn(row_id, rows(app), task["title"])


class InventoryTests(unittest.TestCase):
    INV = maint.parse_inventory(CONFIG["batteries"])

    def test_every_device_and_exclusion_line_is_kept_and_no_live_battery_sensor_is_unlisted(self):
        self.assertEqual(len(self.INV["devices"]), len(CONFIG["batteries"]["devices"]))
        self.assertEqual(len(self.INV["excluded"]), len(CONFIG["batteries"]["excluded"]))
        self.assertEqual(maint.unlisted_batteries(LIVE_BATTERIES, self.INV), [])
        self.assertEqual({d["model"] for d in self.INV["devices"].values()}, set(self.INV["models"]))

    def test_start_up_names_unlisted_battery_sensors(self):
        quiet = make_app()
        done = f"Battery inventory complete: {len(self.INV['devices'])} listed, {len(self.INV['excluded'])} excluded"
        self.assertIn(("INFO", done), quiet.logs)
        states = base_states()
        states["sensor.new_door_battery"] = full("100", {"device_class": "battery"})
        loud = make_app(states)
        self.assertIn(("WARNING", "Battery sensors neither listed nor excluded: sensor.new_door_battery"), loud.logs)

    def test_rooms_names_and_cell_types(self):
        for entity, device in self.INV["devices"].items():
            self.assertIn(device["area"], AREAS, entity)
            self.assertEqual(core.clean_text(device["name"]), device["name"])
        cells = {key: model["cell"] for key, model in self.INV["models"].items()}
        self.assertEqual(cells, {
            "zg204zm": "LADDA AAA NiMH", "zg204zk": "LADDA AAA NiMH", "parasoll": "1x LADDA AAA NiMH",
            "sq610rfnh": "built-in rechargeable", "yale": "lithium AA (rechargeable)", "blind": "", "ws90": "2x AA",
            "twinguard": "6x AA alkaline", "fp300": "2x CR2450", "snzb04p": "CR2477", "snzb02p": "CR2477",
            "zg102zm": "", "hhccjcy01": "CR2032",
        })
        actions = {key: model["action"] for key, model in self.INV["models"].items()}
        self.assertEqual({k for k, a in actions.items() if a == "charge"}, {"sq610rfnh", "blind"})
        self.assertEqual({k for k, a in actions.items() if a == "recharge"}, {"zg204zm", "zg204zk", "parasoll", "yale"})
        self.assertEqual(self.INV["devices"]["sensor.yale_battery"]["history_from"].isoformat(), "2026-02-22")
        self.assertEqual(self.INV["devices"]["sensor.ecowitt_battery_8351"]["history_from"].isoformat(), "2026-08-30")

    def test_the_plant_sensors_batteries_are_cr2032_cells_that_open_at_ten_percent_after_two_hours(self):
        model = self.INV["models"]["hhccjcy01"]
        self.assertEqual((model["cell"], model["action"], model["low"], model["trend"]),
                         ("CR2032", "replace", 10, False))
        sensors = {entity: device for entity, device in self.INV["devices"].items() if device["model"] == "hhccjcy01"}
        self.assertTrue(sensors)
        states = base_states()
        for entity in sensors:
            states[entity] = full("10")
        app = make_app(states)
        for entity in sensors:
            self.assertNotIn(f"maint:battery:{entity}", rows(app), entity)
        tick(app, 2 * HOUR)
        for entity, device in sensors.items():
            row = rows(app)[f"maint:battery:{entity}"]
            self.assertEqual((row["title"], row["detail"], row["area"]),
                             (f"{device['name']} battery", "replace, CR2032", device["area"]), entity)

    def test_the_ws90_backup_cells_are_one_removable_line(self):
        text = (APPS / "home_pulse" / "house_upcoming.yaml").read_text()
        lines = [line for line in text.splitlines() if "ecowitt_battery_8351" in line]
        self.assertEqual(len(lines), 1)
        without = yaml.safe_load(text.replace(lines[0] + "\n", ""))["HouseUpcoming"]["batteries"]
        self.assertEqual(len(maint.parse_inventory(without)["devices"]), len(self.INV["devices"]) - 1)

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
