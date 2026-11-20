# tests/test_kitchen_cook.py - KitchenCook: who is cooking, read from the residents' phones on Bluetooth.
# The pure pieces come first (time held in an area, the session, the windows, the rule); the app is then
# driven through its real initialize() with a fake clock, a scheduler that only records and fake states.
# The fake app has no call_service, so anything but publishing its own entity would fail.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import kitchen_cook as kc  # noqa: E402

TZ = timezone(timedelta(hours=1))
T0 = datetime(2026, 11, 20, 18, 0, tzinfo=TZ).timestamp()
K = "Kitchen"


def track(points):
    t = kc.Track()
    for ts, state in points:
        t.add(T0 + ts, state)
    return t


def held(points, state, lo, hi):
    return track(points).seconds_in(state, T0 + lo, T0 + hi)


class SecondsIn(unittest.TestCase):
    def test_a_state_held_through_the_window(self):
        self.assertEqual(held([(-1000, K)], K, 100, 400), 300)

    def test_only_the_part_of_the_window_it_was_held(self):
        self.assertEqual(held([(0, "Hallway"), (150, K), (300, "Hallway")], K, 100, 400), 150)
        self.assertEqual(held([(0, K), (100, "Hallway"), (200, K), (250, "Hallway")], K, 0, 400), 150)

    def test_the_time_before_the_first_update_counts_as_none(self):
        self.assertEqual(held([(200, K)], K, 100, 400), 200)
        self.assertEqual(held([], K, 100, 400), 0)

    def test_updates_after_the_window_do_not_count(self):
        self.assertEqual(held([(0, K), (500, "Hallway")], K, 100, 400), 300)
        self.assertEqual(held([(450, K)], K, 100, 400), 0)

    def test_other_areas_and_unheard_phones_are_elsewhere(self):
        for state in ("Dining room", "unknown", "unavailable", None, "kitchen"):
            with self.subTest(state=state):
                self.assertEqual(held([(0, state)], K, 100, 400), 0)

    def test_old_updates_are_dropped_but_the_state_held_at_the_cutoff_is_kept(self):
        t = kc.Track()
        for i in range(0, 100):
            t.add(T0 + i * 100, K if i % 2 else "Hallway")
        self.assertLessEqual(len(t.points), kc.KEEP_S // 100 + 2)
        now = T0 + 99 * 100
        self.assertEqual(t.seconds_in(K, now - kc.WINDOW_S, now), 600)


class CookingAndSession(unittest.TestCase):
    def test_any_power_sensor_at_the_threshold_or_above_is_cooking(self):
        self.assertTrue(kc.cooking([0.0, 50.0, None], "off"))
        self.assertTrue(kc.cooking([1500.0], None))
        self.assertFalse(kc.cooking([0.0, 49.9, None], "off"))
        self.assertFalse(kc.cooking([], None))

    def test_the_cooker_heating_is_cooking_and_nothing_else_it_says_is(self):
        self.assertTrue(kc.cooking([0.0], "heat"))
        for state in ("off", "unavailable", "unknown", None):
            with self.subTest(state=state):
                self.assertFalse(kc.cooking([0.0], state))

    def test_the_session_starts_when_cooking_turns_on_and_ends_after_ten_minutes_off(self):
        s = kc.Session()
        self.assertFalse(s.update(100, False))
        self.assertTrue(s.update(200, True))
        self.assertEqual(s.start, 200)
        self.assertTrue(s.update(260, False))
        self.assertTrue(s.update(260 + kc.END_S - 1, False))
        self.assertFalse(s.update(260 + kc.END_S, False))
        self.assertIsNone(s.start)

    def test_cooking_again_within_the_hold_continues_the_session_and_restarts_the_clock(self):
        s = kc.Session()
        s.update(0, True)
        s.update(400, False)
        self.assertTrue(s.update(500, True))
        self.assertEqual(s.start, 0)
        s.update(600, False)
        self.assertTrue(s.update(600 + kc.END_S - 1, False))
        self.assertFalse(s.update(600 + kc.END_S, False))

    def test_a_new_session_starts_fresh_after_the_old_one_ended(self):
        s = kc.Session()
        s.update(0, True)
        s.update(100, False)
        self.assertFalse(s.update(100 + kc.END_S, False))
        self.assertTrue(s.update(5000, True))
        self.assertEqual(s.start, 5000)


class Windows(unittest.TestCase):
    def test_a_young_session_is_read_from_five_minutes_before_its_start(self):
        self.assertEqual(kc.session_window(1000, 1060), (700, 1060))

    def test_an_old_session_is_read_over_the_last_twenty_minutes_only(self):
        self.assertEqual(kc.session_window(1000, 1000 + 3600), (1000 + 3600 - 1200, 1000 + 3600))

    def test_a_cook_is_read_to_five_minutes_after_its_start_or_to_now_when_that_is_sooner(self):
        self.assertEqual(kc.start_window(1000, 1100), (700, 1100))
        self.assertEqual(kc.start_window(1000, 1300), (700, 1300))
        self.assertEqual(kc.start_window(1000, 9000), (700, 1300))


class Pick(unittest.TestCase):
    def test_a_clear_leader_is_the_cook(self):
        self.assertEqual(kc.pick({"mikkel": 0.1, "claudia": 0.9, "kristine": 0.0}), ("claudia", "leads"))

    def test_the_thresholds_themselves_are_enough(self):
        self.assertEqual(kc.pick({"mikkel": 0.6, "claudia": 0.3}), ("mikkel", "leads"))
        self.assertEqual(kc.pick({"mikkel": 0.6}), ("mikkel", "leads"))

    def test_below_sixty_percent_is_nobody(self):
        self.assertEqual(kc.pick({"mikkel": 0.59, "claudia": 0.0}), (None, "too_little"))
        self.assertEqual(kc.pick({"mikkel": 0.0, "claudia": 0.0}), (None, "too_little"))

    def test_a_leader_not_twice_the_next_is_nobody(self):
        self.assertEqual(kc.pick({"mikkel": 0.8, "claudia": 0.5}), (None, "no_lead"))
        self.assertEqual(kc.pick({"mikkel": 1.0, "claudia": 1.0}), (None, "no_lead"))

    def test_nobody_home_is_nobody(self):
        self.assertEqual(kc.pick({}), (None, "no_one_home"))


MIKKEL_AREA, CLAUDIA_AREA, KRISTINE_AREA = "sensor.mikkel_area", "sensor.claudia_area", "sensor.kristine_area"
PERSONS = {"mikkel": "person.mikkel", "claudia": "person.claudia", "kristine": "person.kristine"}
AREAS = {"mikkel": MIKKEL_AREA, "claudia": CLAUDIA_AREA, "kristine": KRISTINE_AREA}
HOB, OVEN, COOKER, ENTITY = "sensor.hob_power", "sensor.oven_power", "climate.cooker", "sensor.kitchen_cook"


def utc(seconds):
    return datetime.fromtimestamp(T0 + seconds, timezone.utc).isoformat()


def iso(seconds):
    return datetime.fromtimestamp(T0 + seconds, TZ).isoformat(timespec="seconds")


def make_app(states=None, changed=None, **args):
    app = kc.KitchenCook.__new__(kc.KitchenCook)
    app.args = {"entity": ENTITY, "kitchen_area": K, "power_w": 50, "power_sensors": [HOB, OVEN],
                "cooker": COOKER, **args,
                "residents": [{"name": n, "person": PERSONS[n], "area_sensor": AREAS[n]} for n in PERSONS]}
    app.states = {MIKKEL_AREA: "Bedroom", CLAUDIA_AREA: "Claudias Room", KRISTINE_AREA: "unknown",
                  PERSONS["mikkel"]: "home", PERSONS["claudia"]: "home", PERSONS["kristine"]: "home",
                  HOB: "0.0", OVEN: "1.0", COOKER: "off", **(states or {})}
    app.changed = changed or {}
    app.clock = {"now": T0}
    app.published, app.logged = [], []

    def get_state(entity, attribute=None, **kw):
        if attribute == "last_changed":
            return utc(app.changed.get(entity, -3600)) if entity in app.states else None
        return app.states.get(entity)

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        app.published.append((entity, state, attributes))
        app.states[entity] = state

    app.get_state = get_state
    app.set_state = set_state
    app.get_now = lambda: datetime.fromtimestamp(app.clock["now"], TZ)
    app._now = lambda: app.clock["now"]
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    app.log = lambda message, level="INFO": app.logged.append((level, message))
    app.initialize()
    return app


def at(app, seconds, entity, new):
    app.clock["now"] = T0 + seconds
    old = app.states.get(entity)
    app.states[entity] = new
    if entity in AREAS.values():
        app._on_area(entity, None, old, new, {})
    elif entity in PERSONS.values():
        app._on_person(entity, None, old, new, {})
    elif entity == COOKER:
        app._on_cooker(entity, None, old, new, {})
    else:
        app._on_power(entity, None, old, new, {})


def tick(app, until, step=30):
    """Run the periodic tick every `step` seconds, and once at `until` (seconds after T0)."""
    t = (app.clock["now"] - T0) // step * step + step
    while t < until:
        app.clock["now"] = T0 + t
        app._on_tick({})
        t += step
    app.clock["now"] = T0 + until
    app._on_tick({})


def last(app):
    entity, state, attributes = app.published[-1]
    return state, attributes


class App(unittest.TestCase):
    def test_starts_idle_and_listens_to_every_input(self):
        app = make_app()
        (entity, state, attributes), = app.published
        self.assertEqual((entity, state, attributes["session_start"], attributes["shares"], attributes["reason"]),
                         (ENTITY, "idle", None, {}, "no_session"))
        watched = {c.args[1] for c in app.listen_state.call_args_list}
        self.assertEqual(watched, set(AREAS.values()) | set(PERSONS.values()) | {HOB, OVEN, COOKER})
        app.run_every.assert_called_once()
        self.assertEqual(app.run_every.call_args.args[1:], (f"now+{kc.TICK_S}", kc.TICK_S))
        self.assertEqual([c.args[1] for c in app.listen_event.call_args_list], ["plugin_started"])

    def test_a_session_nobody_stands_in_the_kitchen_for_is_unknown(self):
        app = make_app()
        at(app, 10, HOB, "1800.0")
        state, attributes = last(app)
        self.assertEqual((state, attributes["session_start"], attributes["reason"]),
                         ("unknown", iso(10), "too_little"))
        self.assertEqual(attributes["shares"], {"mikkel": 0.0, "claudia": 0.0, "kristine": 0.0})

    def test_the_resident_in_the_kitchen_since_before_the_start_is_named_in_lower_case(self):
        app = make_app()
        at(app, -400, CLAUDIA_AREA, K)
        at(app, 0, HOB, "1800.0")
        at(app, 20, MIKKEL_AREA, "Hallway")
        tick(app, 60)
        state, attributes = last(app)
        self.assertEqual((state, attributes["reason"], attributes["session_start"]), ("claudia", "leads", iso(0)))
        self.assertEqual(attributes["shares"], {"mikkel": 0.0, "claudia": 1.0, "kristine": 0.0})

    def test_a_resident_who_arrives_at_the_hob_after_the_start_is_named_once_the_share_is_enough(self):
        app = make_app()
        at(app, 0, HOB, "1800.0")
        at(app, 60, MIKKEL_AREA, K)
        tick(app, 90)
        self.assertEqual(last(app)[0], "unknown")
        tick(app, 720)
        state, attributes = last(app)
        self.assertEqual(state, "mikkel")
        self.assertEqual(attributes["shares"]["mikkel"], round(660 / 1020, 2))

    def test_two_residents_cooking_together_are_nobody(self):
        app = make_app()
        at(app, -400, MIKKEL_AREA, K)
        at(app, -400, KRISTINE_AREA, K)
        at(app, 0, HOB, "1800.0")
        tick(app, 120)
        state, attributes = last(app)
        self.assertEqual((state, attributes["reason"]), ("unknown", "no_lead"))

    def test_a_resident_who_is_not_home_is_not_counted(self):
        app = make_app()
        at(app, -400, MIKKEL_AREA, K)
        at(app, -400, CLAUDIA_AREA, K)
        at(app, -300, PERSONS["mikkel"], "not_home")
        at(app, 0, HOB, "1800.0")
        tick(app, 60)
        state, attributes = last(app)
        self.assertEqual(state, "claudia")
        self.assertNotIn("mikkel", attributes["shares"])

    def test_nobody_home_is_unknown(self):
        app = make_app()
        for person in PERSONS.values():
            at(app, -10, person, "not_home")
        at(app, 0, HOB, "1800.0")
        state, attributes = last(app)
        self.assertEqual((state, attributes["shares"], attributes["reason"]), ("unknown", {}, "no_one_home"))

    def test_the_session_ends_ten_minutes_after_the_power_dropped_and_goes_idle(self):
        app = make_app()
        at(app, -400, CLAUDIA_AREA, K)
        at(app, 0, HOB, "1800.0")
        tick(app, 300)
        at(app, 300, HOB, "0.0")
        tick(app, 300 + kc.END_S - 30)
        self.assertEqual(last(app)[0], "claudia")
        tick(app, 300 + kc.END_S)
        state, attributes = last(app)
        self.assertEqual((state, attributes["session_start"], attributes["shares"], attributes["reason"]),
                         ("idle", None, {}, "no_session"))

    def test_the_cooker_heating_starts_a_session_and_switching_it_off_ends_it(self):
        app = make_app()
        at(app, 0, COOKER, "heat")
        self.assertEqual(last(app)[1]["session_start"], iso(0))
        at(app, 3600, COOKER, "off")
        tick(app, 3600 + kc.END_S - 30)
        self.assertEqual(last(app)[1]["session_start"], iso(0))
        tick(app, 3600 + kc.END_S)
        self.assertEqual(last(app)[0], "idle")

    def test_a_second_appliance_within_the_hold_continues_the_session(self):
        app = make_app()
        at(app, 0, HOB, "1800.0")
        at(app, 100, HOB, "0.0")
        at(app, 500, OVEN, "900.0")
        at(app, 800, OVEN, "1.0")
        tick(app, 800 + kc.END_S - 30)
        self.assertEqual(last(app)[1]["session_start"], iso(0))
        tick(app, 800 + kc.END_S)
        self.assertEqual(last(app)[0], "idle")

    def test_power_below_the_threshold_and_dead_sensors_start_nothing(self):
        app = make_app()
        at(app, 10, HOB, "49.9")
        at(app, 20, OVEN, "unavailable")
        at(app, 30, HOB, "unknown")
        at(app, 40, COOKER, "unavailable")
        self.assertEqual(len(app.published), 1)
        self.assertEqual(last(app)[0], "idle")

    def test_only_the_last_twenty_minutes_of_a_long_session_count(self):
        app = make_app()
        at(app, -300, MIKKEL_AREA, K)
        at(app, 0, HOB, "1800.0")
        at(app, 60, MIKKEL_AREA, "Dining room")
        at(app, 1800, CLAUDIA_AREA, K)
        tick(app, 2400)
        state, attributes = last(app)
        self.assertEqual((state, attributes["shares"]["claudia"], attributes["shares"]["mikkel"]),
                         ("unknown", 0.5, 0.0))
        tick(app, 2760)
        self.assertEqual(last(app)[0], "claudia")

    def test_nothing_is_published_while_nothing_changes(self):
        app = make_app()
        tick(app, 600)
        self.assertEqual(len(app.published), 1)
        at(app, 700, HOB, "1800.0")
        count = len(app.published)
        tick(app, 1200)
        self.assertEqual(len(app.published), count)

    def test_a_home_assistant_restart_republishes_the_unchanged_state(self):
        app = make_app()
        app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(app.published), 2)
        self.assertEqual(app.published[0], app.published[1])

    def test_state_changes_are_logged_once_each(self):
        app = make_app()
        at(app, -400, CLAUDIA_AREA, K)
        at(app, 0, HOB, "1800.0")
        tick(app, 600)
        messages = [m for _, m in app.logged if m.startswith("KitchenCook: ")]
        self.assertEqual([m.split(" ")[1] for m in messages], ["idle", "claudia"])

    def test_a_restart_in_the_middle_of_cooking_reads_the_spell_the_sensor_has_held_since_before(self):
        app = make_app({CLAUDIA_AREA: K, HOB: "1500.0"}, changed={CLAUDIA_AREA: -1500})
        state, attributes = last(app)
        self.assertEqual((state, attributes["shares"]["claudia"], attributes["session_start"]),
                         ("claudia", 1.0, iso(0)))

    def test_a_seed_never_dates_from_the_future(self):
        app = make_app({CLAUDIA_AREA: K}, changed={CLAUDIA_AREA: 500})
        self.assertEqual(app._tracks["claudia"].points[0][0], T0)

    def test_a_bad_power_threshold_fails_at_start(self):
        with self.assertRaises(ValueError):
            make_app(power_w=0)

    def test_the_app_never_calls_a_service(self):
        source = Path(kc.__file__).read_text()
        for call in ("call_service", "turn_on", "turn_off", "notify", "fire_event"):
            self.assertNotIn(call, source)


class CookFor(unittest.TestCase):
    def test_names_the_resident_in_the_kitchen_around_a_start_without_any_session(self):
        app = make_app()
        at(app, -400, CLAUDIA_AREA, K)
        at(app, 250, CLAUDIA_AREA, "Hallway")
        app.clock["now"] = T0 + 700
        self.assertEqual(app.cook_for(T0), "claudia")
        self.assertEqual(len(app.published), 1)

    def test_a_young_start_is_read_to_now_and_the_answer_can_change_as_the_window_fills(self):
        app = make_app()
        at(app, -300, CLAUDIA_AREA, K)
        at(app, 0, CLAUDIA_AREA, "Hallway")
        at(app, 0, KRISTINE_AREA, K)
        app.clock["now"] = T0 + 10
        self.assertEqual(app.cook_for(T0), "claudia")
        app.clock["now"] = T0 + 700
        self.assertIsNone(app.cook_for(T0))

    def test_nobody_in_the_kitchen_is_none(self):
        app = make_app()
        app.clock["now"] = T0 + 400
        self.assertIsNone(app.cook_for(T0))

    def test_a_resident_who_is_not_home_when_asked_is_not_named(self):
        app = make_app()
        at(app, -400, MIKKEL_AREA, K)
        at(app, 350, PERSONS["mikkel"], "not_home")
        self.assertIsNone(app.cook_for(T0))

    def test_a_start_older_than_the_history_kept_is_none(self):
        app = make_app()
        at(app, -400, CLAUDIA_AREA, K)
        at(app, 300, CLAUDIA_AREA, "Hallway")
        for i in range(1, 40):
            at(app, 300 + i * 100, CLAUDIA_AREA, K if i % 2 else "Hallway")
        app.clock["now"] = T0 + 4300
        self.assertIsNone(app.cook_for(T0))

    def test_a_start_that_is_not_a_number_is_none(self):
        app = make_app()
        self.assertIsNone(app.cook_for(None))
        self.assertIsNone(app.cook_for("soon"))


if __name__ == "__main__":
    unittest.main()
