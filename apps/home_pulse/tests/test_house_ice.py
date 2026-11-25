# tests/test_house_ice.py - the ice bath on the real HouseUpcoming app and its existing fake AppDaemon surface,
# with the real house_upcoming.yaml and mobile_notifier.yaml: the thin and thick row, the one push per freeze-up to the
# ice_bath category, the real night of 25.11.2026 and a restart in the middle of a cold spell.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import json
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from test_house_upcoming import APPS, CONFIG, MIN, HOUR, base_states, drain, fire, full, make_app, rows
import upcoming_core as core
import upcoming_ice as ice

SENSOR = "sensor.ice_bath_temperature"
AIR = "sensor.gw2000a_outdoor_temperature"
CLAUDIA = "person.claudia"
LOCAL = ZoneInfo("Europe/Copenhagen")
MIDNIGHT = datetime(2026, 11, 21, 0, 0, tzinfo=LOCAL)
NOTIFIER = yaml.safe_load((APPS / "notify" / "mobile_notifier.yaml").read_text())["MobileNotifier"]
NIGHT = json.loads((Path(__file__).resolve().parent / "fixtures" / "ice_bath_2026_11_25.json").read_text())
CELSIUS = {"unit_of_measurement": "°C"}
QUICK = {"ice_thick_degree_hours": 8}


def at(hour, minute=0, day=21, second=0):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=LOCAL)


def start(water="2.0", air="1.0", claudia="home", now=MIDNIGHT, **kwargs):
    """An app at `now` with the water and the air at the readings given since well before, Claudia as given."""
    states = base_states()
    states[CLAUDIA] = full(claudia)
    states[SENSOR] = full(water, CELSIUS, changed=now - 6 * HOUR)
    states[AIR] = full(air, CELSIUS, changed=now - 6 * HOUR)
    return make_app(states, now=now, **kwargs)


def reading(app, value):
    """The ice bath thermometer reports `value` now."""
    fire(app, SENSOR, full(value, CELSIUS, changed=app.now))
    drain(app)


def outside(app, value):
    """The outdoor thermometer reports `value` now."""
    fire(app, AIR, full(value, CELSIUS, changed=app.now))
    drain(app)


def run_to(app, until):
    """Ticks the app every five minutes up to `until`, and calls what run_daily was asked to at its time."""
    while app.now < until:
        app.now += 5 * MIN
        if app.now.astimezone(LOCAL).time() == app.ice_cfg["push_from"]:
            app._on_ice_hours({})
        app._on_tick({})
        drain(app)


def ice_row(app):
    return rows(app).get("ice:bath")


def pushes(app):
    return app.notifier.sent


def saved(app):
    return json.loads(Path(app.state_file).read_text())["ice"]


class WiringTests(unittest.TestCase):
    def test_the_shipped_settings_are_read_and_the_thermometer_the_air_and_the_person_are_sources(self):
        app = start()
        self.assertEqual(app.ice_cfg, ice.parse(CONFIG))
        self.assertEqual((app.ice_cfg["air"], app.ice_cfg["thick_at"]), (AIR, 80.0))
        self.assertIn((SENSOR, {"attribute": "all"}), app.listened)
        self.assertIn((AIR, {"attribute": "all"}), app.listened)
        self.assertIn((CLAUDIA, {"attribute": "all"}), app.listened)

    def test_the_hours_callback_runs_daily_when_the_push_hours_begin(self):
        self.assertIn(("_on_ice_hours", time(7)), start().daily)

    def test_the_push_category_is_scoped_to_claudia_in_the_shipped_notifier(self):
        self.assertEqual(ice.CATEGORY, "ice_bath")
        self.assertEqual(NOTIFIER["category_audience"][ice.CATEGORY], ["claudia"])
        self.assertIn("person.claudia", NOTIFIER["person_entities"])
        self.assertIn("claudia", NOTIFIER["device_mapping"])

    def test_nothing_about_the_ice_bath_shows_while_the_water_is_warm(self):
        app = start(air="-8.0")
        run_to(app, at(12, day=22))
        self.assertIsNone(ice_row(app))
        self.assertEqual(pushes(app), [])
        self.assertEqual(saved(app), {})

    def test_the_air_alone_starts_nothing(self):
        app = start(water="3.0", air="-12.0")
        outside(app, "-15.0")
        run_to(app, at(0, day=22))
        self.assertIsNone(ice_row(app))
        self.assertEqual(saved(app), {})


class RowTests(unittest.TestCase):
    def test_the_row_comes_two_hours_into_the_cold_as_thin_ice_and_follows_the_reading(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(1, 55))
        self.assertIsNone(ice_row(app))
        run_to(app, at(2, 0))
        row = ice_row(app)
        self.assertEqual((row["title"], row["kind"], row["area"], row["icon"], row["level"]),
                         ("Ice bath: thin ice at 0.3 C since 00:00", "rain", "rooftop", "mdi:snowflake", "thin"))
        for field in ("at", "audience", "audience_users"):
            self.assertNotIn(field, row)
        self.assertTrue(core.is_valid_row(row))
        reading(app, "0.1")
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.1 C since 00:00")

    def test_the_row_stays_up_to_half_a_degree_and_goes_above_it(self):
        app = start()
        reading(app, "0.2")
        run_to(app, at(3, 0))
        reading(app, "0.4")
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.4 C since 00:00")
        reading(app, "0.5")
        self.assertIsNotNone(ice_row(app))
        reading(app, "0.6")
        self.assertIsNone(ice_row(app))
        reading(app, "0.2")
        run_to(app, at(4, 55))
        self.assertIsNone(ice_row(app))
        run_to(app, at(5, 0))
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.2 C since 03:00")

    def test_a_reading_just_above_the_threshold_before_two_hours_starts_the_two_hours_again(self):
        app = start()
        reading(app, "0.2")
        run_to(app, at(1, 30))
        reading(app, "0.4")
        reading(app, "0.2")
        run_to(app, at(2, 0))
        self.assertIsNone(ice_row(app))
        run_to(app, at(3, 30))
        self.assertIsNotNone(ice_row(app))

    def test_unavailable_and_unknown_readings_change_nothing(self):
        app = start()
        reading(app, "0.2")
        run_to(app, at(0, 30))
        reading(app, "unavailable")
        run_to(app, at(1, 0))
        reading(app, "unknown")
        run_to(app, at(2, 0))
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.2 C since 00:00")
        reading(app, "unavailable")
        run_to(app, at(6, 0))
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.2 C since 00:00")

    def test_a_cold_spell_that_began_the_day_before_names_its_day(self):
        app = start()
        reading(app, "0.1")
        run_to(app, at(8, 0))
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.1 C since 00:00")
        run_to(app, at(0, 5, day=22))
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.1 C since 21.11 00:00")

    def test_the_row_turns_thick_when_the_degree_hours_reach_the_setting_and_thin_again_in_a_thaw(self):
        app = start(air="-2.0", **QUICK)
        reading(app, "0.1")
        run_to(app, at(3, 55))
        self.assertEqual(ice_row(app)["level"], "thin")
        run_to(app, at(4, 0))
        row = ice_row(app)
        self.assertEqual((row["title"], row["level"]), ("Ice bath: thick ice at 0.1 C since 00:00", "thick"))
        self.assertTrue(core.is_valid_row(row))
        outside(app, "2.0")
        run_to(app, at(4, 5))
        self.assertEqual((ice_row(app)["title"], ice_row(app)["level"]), ("Ice bath: thin ice at 0.1 C since 00:00", "thin"))

    def test_thick_exactly_forty_hours_into_a_minus_two_freeze_with_the_shipped_setting(self):
        app = start(now=at(3, 0), air="-2.0")
        reading(app, "0.1")
        run_to(app, at(18, 55, day=22))
        self.assertEqual(ice_row(app)["level"], "thin")
        run_to(app, at(19, 0, day=22))
        self.assertEqual(ice_row(app)["level"], "thick")
        self.assertAlmostEqual(app._ice["fdh"], 80.0)

    def test_an_unavailable_air_sensor_adds_no_degree_hours(self):
        app = start(air="-2.0", **QUICK)
        reading(app, "0.1")
        run_to(app, at(1, 0))
        outside(app, "unavailable")
        run_to(app, at(9, 0))
        self.assertEqual(ice_row(app)["level"], "thin")
        self.assertAlmostEqual(app._ice["fdh"], 2.0)
        outside(app, "-2.0")
        run_to(app, at(11, 55))
        self.assertEqual(ice_row(app)["level"], "thin")
        run_to(app, at(12, 0))
        self.assertEqual(ice_row(app)["level"], "thick")

    def test_an_unknown_air_reading_at_start_adds_nothing_either(self):
        app = start(air="unknown", **QUICK)
        reading(app, "0.1")
        run_to(app, at(8, 0))
        self.assertEqual(ice_row(app)["level"], "thin")
        self.assertEqual(app._ice["fdh"], 0.0)
        self.assertNotIn("air", app._ice)

    def test_an_air_reading_between_ticks_counts_from_when_it_came(self):
        app = start(air="-4.0", **QUICK)
        reading(app, "0.1")
        app.now = at(1, 0)
        outside(app, "0.0")
        self.assertAlmostEqual(app._ice["fdh"], 4.0)
        run_to(app, at(5, 0))
        self.assertAlmostEqual(app._ice["fdh"], 4.0)
        outside(app, "-4.0")
        run_to(app, at(5, 30))
        self.assertAlmostEqual(app._ice["fdh"], 6.0)
        run_to(app, at(6, 0))
        self.assertAlmostEqual(app._ice["fdh"], 8.0)


class PushTests(unittest.TestCase):
    EXPECTED = {"title": "Ice bath: thick ice", "message": "At 0.3 °C since 00:00", "target": "home",
                "category": "ice_bath"}

    def cold(self, air="-2.0", water="0.3", settings=QUICK, **kwargs):
        app = start(air=air, **{**settings, **kwargs})
        reading(app, water)
        return app

    def test_thin_ice_alone_is_never_pushed_however_long_it_lasts(self):
        app = self.cold(air="1.0", settings={})
        run_to(app, at(21, 55, day=22))
        self.assertEqual(pushes(app), [])
        self.assertEqual(ice_row(app)["level"], "thin")

    def test_one_push_at_seven_to_the_category_with_the_facts_only(self):
        app = self.cold()
        run_to(app, at(6, 55))
        self.assertEqual(ice_row(app)["level"], "thick")
        self.assertEqual(pushes(app), [])
        run_to(app, at(7, 0))
        self.assertEqual(pushes(app), [self.EXPECTED])
        run_to(app, at(21, 55))
        self.assertEqual(pushes(app), [self.EXPECTED])
        self.assertTrue(any("Ice bath push sent" in msg for _, msg in app.logs))

    def test_it_is_one_push_for_the_freeze_up_not_one_each_morning(self):
        app = self.cold()
        run_to(app, at(21, 55, day=22))
        self.assertEqual([p["message"] for p in pushes(app)], ["At 0.3 °C since 00:00"])

    def test_the_push_goes_the_minute_the_ice_turns_thick_by_day_and_not_before(self):
        app = start(now=at(8), air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(11, 55))
        self.assertEqual(pushes(app), [])
        run_to(app, at(12, 0))
        self.assertEqual(pushes(app), [{**self.EXPECTED, "message": "At 0.3 °C since 08:00"}])

    def test_the_push_at_exactly_forty_hours_with_the_shipped_setting(self):
        app = start(now=at(3, 0), air="-2.0")
        reading(app, "0.1")
        run_to(app, at(18, 55, day=22))
        self.assertEqual(pushes(app), [])
        run_to(app, at(19, 0, day=22))
        self.assertEqual([p["message"] for p in pushes(app)], ["At 0.1 °C since 21.11 03:00"])

    def test_ice_that_turns_thick_in_the_evening_is_pushed_then(self):
        app = start(now=at(16), air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(19, 55))
        self.assertEqual(pushes(app), [])
        run_to(app, at(20, 0))
        self.assertEqual([p["message"] for p in pushes(app)], ["At 0.3 °C since 16:00"])

    def test_nothing_goes_after_ten_in_the_evening_and_it_goes_at_seven(self):
        app = start(now=at(19, 30), air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(23, 55))
        self.assertEqual(ice_row(app)["level"], "thick")
        self.assertEqual(pushes(app), [])
        run_to(app, at(6, 55, day=22))
        self.assertEqual(pushes(app), [])
        run_to(app, at(7, 0, day=22))
        self.assertEqual([p["message"] for p in pushes(app)], ["At 0.3 °C since 21.11 19:30"])

    def test_it_goes_when_she_comes_home_and_not_while_she_is_away(self):
        app = self.cold(claudia="not_home")
        run_to(app, at(12, 0))
        self.assertEqual(pushes(app), [])
        app.now = at(12, 3)
        fire(app, CLAUDIA, full("home", changed=app.now))
        drain(app)
        self.assertEqual(pushes(app), [{**self.EXPECTED}])
        run_to(app, at(21, 55))
        self.assertEqual(len(pushes(app)), 1)

    def test_other_places_are_not_home(self):
        for state in ("Office", "unknown", "unavailable"):
            app = self.cold(claudia=state)
            run_to(app, at(12, 0))
            self.assertEqual(pushes(app), [], state)

    def test_coming_home_at_night_waits_for_the_morning(self):
        app = self.cold(claudia="not_home")
        run_to(app, at(22, 30))
        app.now = at(23, 0)
        fire(app, CLAUDIA, full("home", changed=app.now))
        drain(app)
        run_to(app, at(6, 55, day=22))
        self.assertEqual(pushes(app), [])
        run_to(app, at(7, 0, day=22))
        self.assertEqual([p["message"] for p in pushes(app)], ["At 0.3 °C since 21.11 00:00"])

    def test_leaving_and_coming_home_again_does_not_repeat_the_push(self):
        app = self.cold()
        run_to(app, at(7, 0))
        for state, hour in (("not_home", 9), ("home", 15)):
            app.now = at(hour)
            fire(app, CLAUDIA, full(state, changed=app.now))
            drain(app)
        self.assertEqual(len(pushes(app)), 1)

    def test_a_thaw_and_a_return_of_the_frost_do_not_push_again_while_the_ice_never_went(self):
        app = start(now=at(8), air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(12, 0))
        self.assertEqual(len(pushes(app)), 1)
        outside(app, "2.0")
        run_to(app, at(14, 0))
        self.assertEqual(ice_row(app)["level"], "thin")
        outside(app, "-2.0")
        run_to(app, at(21, 55))
        self.assertEqual(ice_row(app)["level"], "thick")
        self.assertEqual(len(pushes(app)), 1)

    def test_degree_hours_back_at_zero_make_the_next_thick_ice_a_new_push(self):
        app = start(now=at(8), air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(12, 0))
        outside(app, "2.0")
        run_to(app, at(16, 0))
        self.assertAlmostEqual(app._ice["fdh"], 0.0)
        self.assertNotIn("told", app._ice)
        outside(app, "-2.0")
        run_to(app, at(19, 55))
        self.assertEqual(len(pushes(app)), 1)
        run_to(app, at(20, 0))
        self.assertEqual(len(pushes(app)), 2)

    def test_the_spell_ending_makes_the_next_thick_ice_a_new_push(self):
        app = start(now=at(8), air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(12, 0))
        self.assertEqual(len(pushes(app)), 1)
        reading(app, "2.0")
        self.assertIsNone(ice_row(app))
        self.assertEqual(app._ice, {})
        reading(app, "0.3")
        run_to(app, at(15, 55))
        self.assertEqual(len(pushes(app)), 1)
        run_to(app, at(16, 0))
        self.assertEqual(len(pushes(app)), 2)

    def test_a_push_nobody_got_is_tried_again_after_ten_minutes_and_then_not_again(self):
        app = self.cold()
        app.notifier.deliver = 0
        run_to(app, at(7, 0))
        self.assertEqual(len(pushes(app)), 1)
        self.assertTrue(any(level == "WARNING" and "not delivered" in msg for level, msg in app.logs))
        app.notifier.deliver = 1
        run_to(app, at(7, 5))
        self.assertEqual(len(pushes(app)), 1)
        run_to(app, at(7, 10))
        self.assertEqual(len(pushes(app)), 2)
        run_to(app, at(21, 55))
        self.assertEqual(len(pushes(app)), 2)

    def test_a_notifier_that_raises_counts_as_not_delivered(self):
        app = self.cold()
        calls = []

        async def broken(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("HA unreachable")

        app.notifier.notify = broken
        run_to(app, at(7, 0))
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("Ice bath push failed" in msg for _, msg in app.logs))
        run_to(app, at(7, 10))
        self.assertEqual(len(calls), 2)

    def test_the_hours_are_settings(self):
        app = self.cold(ice_push_from="09:00:00", ice_push_until="10:00:00")
        run_to(app, at(8, 55))
        self.assertEqual(pushes(app), [])
        run_to(app, at(9, 0))
        self.assertEqual(len(pushes(app)), 1)


class RestartTests(unittest.TestCase):
    def test_a_restart_in_the_middle_of_the_cold_keeps_the_clock(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(1, 30))
        reading(app, "0.2")
        run_to(app, at(1, 40))
        again = make_app(app.states, state_dir=app.state_dir, now=app.now)
        self.assertIsNone(ice_row(again))
        run_to(again, at(2, 0))
        self.assertEqual(ice_row(again)["title"], "Ice bath: thin ice at 0.2 C since 00:00")

    def test_a_restart_keeps_the_degree_hours_and_the_time_away_adds_none(self):
        app = start(air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(3, 0))
        self.assertAlmostEqual(app._ice["fdh"], 6.0)
        self.assertAlmostEqual(saved(app)["fdh"], 6.0)
        again = make_app(app.states, state_dir=app.state_dir, now=at(3, 30), **QUICK)
        self.assertAlmostEqual(again._ice["fdh"], 6.0)
        self.assertEqual(ice_row(again)["level"], "thin")
        run_to(again, at(4, 25))
        self.assertEqual(ice_row(again)["level"], "thin")
        run_to(again, at(4, 30))
        self.assertEqual(ice_row(again)["level"], "thick")

    def test_a_restart_does_not_repeat_the_push(self):
        app = start(air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(7, 0))
        self.assertEqual(len(pushes(app)), 1)
        self.assertTrue(saved(app)["told"])
        again = make_app(app.states, state_dir=app.state_dir, now=app.now, **QUICK)
        drain(again)
        run_to(again, at(21, 55))
        self.assertEqual(pushes(again), [])

    def test_a_restart_in_the_morning_with_thick_ice_and_no_push_yet_pushes_at_once(self):
        app = start(air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(5, 0))
        self.assertEqual(ice_row(app)["level"], "thick")
        again = make_app(app.states, state_dir=app.state_dir, now=at(7, 30), **QUICK)
        drain(again)
        self.assertEqual([p["message"] for p in pushes(again)], ["At 0.3 °C since 00:00"])

    def test_the_saved_state_is_the_spell_the_degree_hours_the_held_air_and_the_flag(self):
        app = start(air="-2.0", **QUICK)
        reading(app, "0.3")
        run_to(app, at(7, 0))
        state = saved(app)
        self.assertAlmostEqual(state.pop("fdh"), 14.0)
        self.assertEqual(state, {"since": core.utc_iso(MIDNIGHT), "value": 0.3, "air": -2.0,
                                 "air_at": core.utc_iso(at(7, 0)), "told": True})

    def test_without_saved_state_the_spell_starts_where_the_reading_last_changed(self):
        now = at(2, 0)
        states = base_states()
        states[CLAUDIA] = full("home")
        states[SENSOR] = full("0.2", changed=at(1, 30))
        app = make_app(states, now=now)
        self.assertIsNone(ice_row(app))
        run_to(app, at(3, 30))
        self.assertEqual(ice_row(app)["title"], "Ice bath: thin ice at 0.2 C since 01:30")

    def test_a_state_saved_before_degree_hours_existed_is_taken_over_as_thin_ice(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(1, 0))
        Path(app.state_file).write_text(json.dumps({"version": 1, "ice": {
            "since": core.utc_iso(MIDNIGHT), "value": 0.3, "pushed_on": "2026-11-21"}}))
        again = make_app(app.states, state_dir=app.state_dir, now=at(1, 5))
        self.assertEqual(again._ice, {"since": core.utc_iso(MIDNIGHT), "value": 0.3, "fdh": 0.0, "air": 1.0,
                                      "air_at": core.utc_iso(at(1, 5))})
        run_to(again, at(2, 0))
        self.assertEqual(ice_row(again)["level"], "thin")
        self.assertEqual(pushes(again), [])

    def test_bad_saved_state_is_ignored(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(1, 0))
        Path(app.state_file).write_text(json.dumps({"version": 1, "ice": {"since": "yesterday", "value": "cold",
                                                                          "fdh": 99, "told": True}}))
        states = dict(app.states)
        states[SENSOR] = full("2.0", changed=at(1, 0))
        again = make_app(states, state_dir=app.state_dir, now=at(1, 5))
        run_to(again, at(12, 0))
        self.assertIsNone(ice_row(again))
        self.assertEqual(pushes(again), [])


class RealNightTests(unittest.TestCase):
    """The night of 24-25.11.2026 through the app, event by event, with the five-minute tick between: the water flickered
    between 0.3 and 0.4 for hours, the air stayed between -0.1 and +2.1 C, and the 07:00 push of the thin rule went to
    Claudia, who found only a little thin ice."""

    @staticmethod
    def timeline(first):
        begin = datetime.fromisoformat(NIGHT["start"])
        events = [(begin + timedelta(seconds=s), SENSOR, v) for s, v in NIGHT["water"]]
        events += [(begin + timedelta(seconds=s), AIR, v) for s, v in NIGHT["air"]]
        ticks = [(first + 5 * MIN * i, None, None) for i in range(1, 12 * 12 + 1)]
        return sorted(events + ticks, key=lambda item: item[0])

    def test_the_thin_row_comes_at_ten_to_seven_with_no_push_and_no_degree_hours(self):
        first = at(22, 0, day=24)
        app = start(now=first, water="0.5", air="0.8", claudia="home")
        shown, seen_hours = [], False
        for when, entity, value in self.timeline(first):
            if when > at(9, 0, day=25):
                break
            app.now = when
            if entity is None:
                if when.astimezone(LOCAL).time() == time(7):
                    app._on_ice_hours({})
                app._on_tick({})
            else:
                fire(app, entity, full(str(value), CELSIUS, changed=when))
            drain(app)
            row = ice_row(app)
            if row:
                shown.append((when, row["title"], row["level"]))
        self.assertEqual(pushes(app), [])
        self.assertTrue(at(6, 49, day=25, second=55) <= shown[0][0] <= at(6, 50, day=25, second=30), shown[0])
        self.assertEqual({level for _, _, level in shown}, {"thin"})
        self.assertTrue(shown[0][1].startswith("Ice bath: thin ice at 0.") and shown[0][1].endswith("C since 04:49"))
        self.assertEqual(app._ice["fdh"], 0.0)
        self.assertEqual(saved(app)["fdh"], 0.0)


if __name__ == "__main__":
    unittest.main()
