# tests/test_house_ice.py - the ice bath on the real HouseUpcoming app and its existing fake AppDaemon surface,
# with the real house_upcoming.yaml and mobile_notifier.yaml: the row, the one push a day to the ice_bath
# category and a restart in the middle of a cold spell.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import json
import unittest
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from test_house_upcoming import APPS, CONFIG, MIN, HOUR, base_states, drain, fire, full, make_app, rows
import upcoming_core as core
import upcoming_ice as ice

SENSOR = "sensor.ice_bath_temperature"
CLAUDIA = "person.claudia"
LOCAL = ZoneInfo("Europe/Copenhagen")
MIDNIGHT = datetime(2026, 11, 21, 0, 0, tzinfo=LOCAL)
NOTIFIER = yaml.safe_load((APPS / "notify" / "mobile_notifier.yaml").read_text())["MobileNotifier"]


def at(hour, minute=0, day=21):
    return datetime(2026, 11, day, hour, minute, tzinfo=LOCAL)


def start(water="2.0", claudia="home", now=MIDNIGHT, **kwargs):
    """An app at `now` with the water at `water` since well before, Claudia as given."""
    states = base_states()
    states[CLAUDIA] = full(claudia)
    states[SENSOR] = full(water, {"unit_of_measurement": "°C"}, changed=now - 6 * HOUR)
    return make_app(states, now=now, **kwargs)


def reading(app, value):
    """The thermometer reports `value` now."""
    fire(app, SENSOR, full(value, {"unit_of_measurement": "°C"}, changed=app.now))
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


class WiringTests(unittest.TestCase):
    def test_the_shipped_settings_are_read_and_the_thermometer_and_the_person_are_sources(self):
        app = start()
        self.assertEqual(app.ice_cfg, ice.parse(CONFIG))
        self.assertIn((SENSOR, {"attribute": "all"}), app.listened)
        self.assertIn((CLAUDIA, {"attribute": "all"}), app.listened)

    def test_the_hours_callback_runs_daily_when_the_push_hours_begin(self):
        self.assertIn(("_on_ice_hours", time(7)), start().daily)

    def test_the_push_category_is_scoped_to_claudia_in_the_shipped_notifier(self):
        self.assertEqual(ice.CATEGORY, "ice_bath")
        self.assertEqual(NOTIFIER["category_audience"][ice.CATEGORY], ["claudia"])
        self.assertIn("person.claudia", NOTIFIER["person_entities"])
        self.assertIn("claudia", NOTIFIER["device_mapping"])

    def test_nothing_about_the_ice_bath_shows_while_the_water_is_warm(self):
        app = start()
        run_to(app, at(12, day=22))
        self.assertIsNone(ice_row(app))
        self.assertEqual(pushes(app), [])
        self.assertEqual(json.loads(Path(app.state_file).read_text())["ice"], {})


class RowTests(unittest.TestCase):
    def test_the_row_comes_two_hours_into_the_cold_and_follows_the_reading(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(1, 55))
        self.assertIsNone(ice_row(app))
        run_to(app, at(2, 0))
        row = ice_row(app)
        self.assertEqual((row["title"], row["kind"], row["area"], row["icon"]),
                         ("Ice bath at 0.3 C since 00:00", "rain", "rooftop", "mdi:snowflake"))
        for field in ("at", "audience", "audience_users"):
            self.assertNotIn(field, row)
        self.assertTrue(core.is_valid_row(row))
        reading(app, "0.1")
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.1 C since 00:00")

    def test_the_row_stays_up_to_half_a_degree_and_goes_above_it(self):
        app = start()
        reading(app, "0.2")
        run_to(app, at(3, 0))
        reading(app, "0.4")
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.4 C since 00:00")
        reading(app, "0.5")
        self.assertIsNotNone(ice_row(app))
        reading(app, "0.6")
        self.assertIsNone(ice_row(app))
        reading(app, "0.2")
        run_to(app, at(4, 55))
        self.assertIsNone(ice_row(app))
        run_to(app, at(5, 0))
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.2 C since 03:00")

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
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.2 C since 00:00")
        reading(app, "unavailable")
        run_to(app, at(6, 0))
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.2 C since 00:00")

    def test_a_cold_spell_that_began_the_day_before_names_its_day(self):
        app = start()
        reading(app, "0.1")
        run_to(app, at(8, 0))
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.1 C since 00:00")
        run_to(app, at(0, 5, day=22))
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.1 C since 21.11 00:00")


class PushTests(unittest.TestCase):
    EXPECTED = {"title": "Ice bath", "message": "At 0.3 °C since 00:00", "target": "home", "category": "ice_bath"}

    def cold(self, **kwargs):
        app = start(**kwargs)
        reading(app, "0.3")
        return app

    def test_one_push_at_seven_to_the_category_with_the_facts_only(self):
        app = self.cold()
        run_to(app, at(6, 55))
        self.assertEqual(pushes(app), [])
        run_to(app, at(7, 0))
        self.assertEqual(pushes(app), [self.EXPECTED])
        run_to(app, at(21, 55))
        self.assertEqual(pushes(app), [self.EXPECTED])
        self.assertTrue(any("Ice bath push sent" in msg for _, msg in app.logs))

    def test_again_the_next_morning_while_it_lasts_and_not_once_the_water_is_warm(self):
        app = self.cold()
        run_to(app, at(7, 0, day=22))
        self.assertEqual([p["message"] for p in pushes(app)], ["At 0.3 °C since 00:00", "At 0.3 °C since 21.11 00:00"])
        reading(app, "2.0")
        run_to(app, at(7, 0, day=23))
        self.assertEqual(len(pushes(app)), 2)

    def test_the_push_waits_in_the_night_and_for_ice_to_be_likely(self):
        app = start(now=at(10))
        reading(app, "0.3")
        run_to(app, at(11, 55))
        self.assertEqual(pushes(app), [])
        run_to(app, at(12, 0))
        self.assertEqual(pushes(app), [{**self.EXPECTED, "message": "At 0.3 °C since 10:00"}])

    def test_nothing_goes_after_ten_in_the_evening_and_it_goes_at_seven(self):
        app = start(now=at(19))
        reading(app, "0.3")
        run_to(app, at(21, 55))
        self.assertEqual(len(pushes(app)), 1)
        app = start(now=at(20, 30))
        reading(app, "0.3")
        run_to(app, at(22, 30))
        self.assertEqual(pushes(app), [])
        run_to(app, at(7, 0, day=22))
        self.assertEqual([p["message"] for p in pushes(app)], ["At 0.3 °C since 21.11 20:30"])

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

    def test_leaving_and_coming_home_again_does_not_repeat_the_days_push(self):
        app = self.cold()
        run_to(app, at(7, 0))
        for state, hour in (("not_home", 9), ("home", 15)):
            app.now = at(hour)
            fire(app, CLAUDIA, full(state, changed=app.now))
            drain(app)
        self.assertEqual(len(pushes(app)), 1)

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
        self.assertEqual(ice_row(again)["title"], "Ice bath at 0.2 C since 00:00")

    def test_a_restart_does_not_repeat_the_days_push(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(7, 0))
        self.assertEqual(len(pushes(app)), 1)
        saved = json.loads(Path(app.state_file).read_text())["ice"]
        self.assertEqual(saved["pushed_on"], "2026-11-21")
        again = make_app(app.states, state_dir=app.state_dir, now=app.now)
        drain(again)
        run_to(again, at(21, 55))
        self.assertEqual(pushes(again), [])

    def test_a_restart_in_the_morning_with_ice_and_no_push_yet_pushes_at_once(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(5, 0))
        again = make_app(app.states, state_dir=app.state_dir, now=at(7, 30))
        drain(again)
        self.assertEqual([p["message"] for p in pushes(again)], ["At 0.3 °C since 00:00"])

    def test_the_saved_state_is_the_spell_the_last_reading_and_the_day_of_the_push(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(7, 0))
        saved = json.loads(Path(app.state_file).read_text())["ice"]
        self.assertEqual(saved, {"since": core.utc_iso(MIDNIGHT), "value": 0.3, "pushed_on": "2026-11-21"})

    def test_without_saved_state_the_spell_starts_where_the_reading_last_changed(self):
        now = at(2, 0)
        states = base_states()
        states[CLAUDIA] = full("home")
        states[SENSOR] = full("0.2", changed=at(1, 30))
        app = make_app(states, now=now)
        self.assertIsNone(ice_row(app))
        run_to(app, at(3, 30))
        self.assertEqual(ice_row(app)["title"], "Ice bath at 0.2 C since 01:30")

    def test_bad_saved_state_is_ignored(self):
        app = start()
        reading(app, "0.3")
        run_to(app, at(1, 0))
        Path(app.state_file).write_text(json.dumps({"version": 1, "ice": {"since": "yesterday", "value": "cold",
                                                                          "pushed_on": 3}}))
        states = dict(app.states)
        states[SENSOR] = full("2.0", changed=at(1, 0))
        again = make_app(states, state_dir=app.state_dir, now=at(1, 5))
        run_to(again, at(12, 0))
        self.assertIsNone(ice_row(again))
        self.assertEqual(pushes(again), [])


if __name__ == "__main__":
    unittest.main()
