# tests/test_dishwasher_delay_timer.py - the Miele's delay timer, seen from closed-door standby power: idle
# draws 0.01-0.02 W, a countdown 0.06-0.09 W, an awake display 0.6-1.5 W. While the timer is set the monitor
# publishes binary_sensor.dishwasher_timer, a programme picked then is kept for the timed run (the finish
# reads it, as it reads a pick made during the run), and a timer that goes away takes the pick with it.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_dishwasher_attribution as attribution  # noqa: E402  (installs the appdaemon stub)
import door_history  # noqa: E402

FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures" / "dishwasher_delay_timer_2026_10_21.json").read_text())
TIMER = "binary_sensor.dishwasher_timer"
SELECTOR = "input_select.dishwasher_confirmed_programme"
SHORT = "input_select.dishwasher_short"
POWER = attribution.POWER_SENSOR
DOOR = attribution.DOOR_SENSOR
STATE_ENTITY = attribution.STATE_ENTITY
T0 = datetime(2026, 10, 21, 19, 0, tzinfo=timezone.utc)


def flat(points, start, minutes, watts):
    """Readings every 10 s at `watts` for `minutes` from `start`."""
    return points + [(start + timedelta(seconds=10 * i), watts) for i in range(minutes * 6)]


class StandbyReading(unittest.TestCase):
    def reading(self, points, now):
        return door_history.standby_reading(points, now, 0.04, 0.3)

    def test_a_countdown_reads_as_a_timer(self):
        pts = flat([], T0, 12, 0.07)
        self.assertEqual(self.reading(pts, T0 + timedelta(minutes=12)), "timer")

    def test_idle_reads_as_idle(self):
        pts = flat([], T0, 12, 0.016)
        self.assertEqual(self.reading(pts, T0 + timedelta(minutes=12)), "idle")

    def test_an_awake_display_says_nothing(self):
        pts = flat([], T0, 12, 1.2)
        self.assertIsNone(self.reading(pts, T0 + timedelta(minutes=12)))

    def test_a_wake_blip_in_idle_is_no_timer(self):
        pts = flat([], T0, 10, 0.016)
        pts = flat(pts, T0 + timedelta(minutes=10), 3, 0.6)
        pts = flat(pts, T0 + timedelta(minutes=13), 1, 0.05)
        pts = flat(pts, T0 + timedelta(minutes=14), 12, 0.016)
        for minute in range(11, 23):
            self.assertIsNone(self.reading(pts, T0 + timedelta(minutes=minute)), minute)
        self.assertEqual(self.reading(pts, T0 + timedelta(minutes=26)), "idle")

    def test_too_little_history_says_nothing(self):
        pts = flat([], T0, 5, 0.07)
        self.assertIsNone(self.reading(pts, T0 + timedelta(minutes=5)))


def make_app(tmp, clock, *, door="off", programme="—", short="—", power_history=()):
    app = attribution.make_app(tmp, clock, attribution.make_actor(clock, []), sensor_state="Off", helper_state="Off")
    app.args.update({"confirmed_programme_entity": SELECTOR, "short_entity": SHORT})
    app.entities[DOOR]["state"] = door
    app.entities[SELECTOR] = {"state": programme, "attributes": {}, "last_changed": None}
    app.entities[SHORT] = {"state": short, "attributes": {}, "last_changed": None}
    app.selects = []

    def call_service(service, **kw):
        if service == "input_select/select_option" and kw["entity_id"] in (SELECTOR, SHORT):
            app.selects.append((kw["entity_id"], kw["option"]))
            app.entities[kw["entity_id"]]["state"] = kw["option"]

    app.call_service = call_service
    app.recorded = {POWER: list(power_history), DOOR: []}

    def get_history(entity_id=None, start_time=None, end_time=None, **kw):
        rows = [{"state": str(v), "last_changed": t.isoformat()} for t, v in app.recorded.get(entity_id, [])
                if end_time is None or t <= end_time]
        return [rows]

    app.get_history = get_history
    app.initialize()
    return app


def power(app, clock, at, watts):
    clock.now = at
    app.recorded[POWER].append((at, watts))
    app.entities[POWER]["state"] = str(watts)
    app._power_changed(POWER, None, None, str(watts), {})


def door(app, clock, at, state):
    clock.now = at
    app.recorded[DOOR].append((at, state))
    old = app.entities[DOOR]["state"]
    app.entities[DOOR]["state"] = state
    app._door_state_changed(DOOR, None, old, state, {})


def replay(app, clock, segment, until_s=None, on_event=None):
    base = datetime.fromisoformat(segment["base"])
    events = [(s, "power", v) for s, v in segment["power"]] + [(s, "door", v) for s, v in segment["door"]]
    events.sort(key=lambda e: (e[0], e[1] == "power"))
    for s, kind, value in events:
        if until_s is not None and s > until_s:
            break
        at = base + timedelta(seconds=s)
        (power if kind == "power" else door)(app, clock, at, value)
        if on_event:
            on_event(at)


def timer(app):
    return app.entities.get(TIMER, {}).get("state")


def pick(app, label):
    old = app.entities[SELECTOR]["state"]
    app.entities[SELECTOR]["state"] = label
    app._on_selector_changed(SELECTOR, "state", old, label, {})


def logged(app, text):
    return [a[0] for a, kw in app.log_calls if text in str(a[0])]


class TimerNight(unittest.TestCase):
    """21.10.2026: the door shut 21:47 local on a timer, someone added a glass 22:30, the run began 02:33."""

    NIGHT = FIXTURE["timer_night"]
    BASE = datetime.fromisoformat(NIGHT["base"])
    TIMER_CLOSE = BASE + timedelta(seconds=1024.7)

    def night_app(self, tmp):
        """Off with the door open, as the evening began; the recorder also holds the idle afternoon
        before it, the baseline the timer's own close is found against."""
        clock = attribution.Clock(self.BASE)
        app = make_app(tmp, clock, door="on", programme="—")
        app.recorded[DOOR] = [(self.BASE - timedelta(hours=3), "on"), (self.BASE - timedelta(hours=3, minutes=-1), "off")]
        app.recorded[POWER] = [(self.BASE - timedelta(hours=3), 0.016)]
        return app, clock

    def test_the_timer_shows_within_half_an_hour_and_holds_through_the_glass_until_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock = self.night_app(tmp)
            seen = []
            replay(app, clock, self.NIGHT, on_event=lambda at: seen.append((at, timer(app))))
            first_on = next(at for at, st in seen if st == "on")
            self.assertLess(first_on - self.TIMER_CLOSE, timedelta(minutes=30))
            self.assertTrue(all(st == "on" for at, st in seen if at >= first_on))
            since = datetime.fromisoformat(app.entities[TIMER]["attributes"]["since"].replace("Z", "+00:00"))
            self.assertTrue(self.BASE <= since <= self.TIMER_CLOSE, since)
            self.assertEqual(app.state, "Off")

    def test_intensive_picked_while_the_timer_runs_is_the_runs_programme(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock = self.night_app(tmp)
            replay(app, clock, self.NIGHT, until_s=3 * 3600)
            self.assertEqual(timer(app), "on")
            pick(app, "Intensive")
            self.assertEqual(logged(app, "ignored"), [])
            self.assertTrue(logged(app, "kept for the timed run"))
            replay(app, clock, {**self.NIGHT, "power": [p for p in self.NIGHT["power"] if p[0] > 3 * 3600], "door": []})
            selects_before_start = list(app.selects)
            attribution.load_and_start(app, clock)
            self.assertEqual(app.selects, selects_before_start)
            self.assertEqual(app.entities[SELECTOR]["state"], "Intensive")
            self.assertEqual(timer(app), "off")
            self.assertEqual(app._get_confirmed_from_selector("eco"), ("intensive", True))
            self.assertEqual(app.detected_programme, "intensive")

    def test_an_appdaemon_restart_mid_countdown_keeps_the_timer_and_the_pick(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock = self.night_app(tmp)
            replay(app, clock, self.NIGHT, until_s=3 * 3600)
            pick(app, "Intensive")
            reloaded = attribution.make_app(tmp, clock, attribution.make_actor(clock, []))
            reloaded.entities = app.entities
            reloaded.get_state = attribution._entity_get_state(app.entities)
            reloaded.args.update({"confirmed_programme_entity": SELECTOR, "short_entity": SHORT})
            reloaded.selects = []
            reloaded.call_service = lambda service, **kw: reloaded.selects.append(kw["entity_id"]) if kw.get("entity_id") in (SELECTOR, SHORT) else None
            reloaded.get_history = app.get_history
            reloaded.initialize()
            self.assertEqual(timer(reloaded), "on")
            self.assertEqual(reloaded.selects, [])
            self.assertEqual(reloaded._get_confirmed_from_selector("eco"), ("intensive", True))

    def test_an_ha_restart_mid_countdown_keeps_the_timer_and_the_pick(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock = self.night_app(tmp)
            replay(app, clock, self.NIGHT, until_s=3 * 3600)
            pick(app, "Intensive")
            entities = {k: v for k, v in app.entities.items() if k not in (STATE_ENTITY, TIMER)}
            entities[STATE_ENTITY] = {"state": None, "attributes": {}, "last_changed": None}
            reloaded = attribution.make_app(tmp, clock, attribution.make_actor(clock, []))
            reloaded.entities = entities
            reloaded.get_state = attribution._entity_get_state(entities)
            reloaded.set_state = _set_state_into(entities, clock)
            reloaded.args.update({"confirmed_programme_entity": SELECTOR, "short_entity": SHORT})
            reloaded.selects = []
            reloaded.call_service = lambda service, **kw: reloaded.selects.append(kw["entity_id"]) if kw.get("entity_id") in (SELECTOR, SHORT) else None
            reloaded.get_history = app.get_history
            reloaded.initialize()
            self.assertEqual(reloaded.state, "Off")
            self.assertEqual(timer(reloaded), "on")
            self.assertEqual(reloaded.selects, [])


def _set_state_into(entities, clock):
    def set_state(entity_id, **kw):
        rec = entities.setdefault(entity_id, {"state": None, "attributes": {}, "last_changed": None})
        if kw.get("state") is not None:
            rec["state"] = kw["state"]
        if kw.get("attributes") is not None:
            rec["attributes"] = dict(kw["attributes"]) if kw.get("replace") else {**rec["attributes"], **kw["attributes"]}
        rec["last_changed"] = clock()
    return set_state


class IdleAndCancel(unittest.TestCase):
    def test_an_idle_afternoon_with_a_display_wake_never_shows_a_timer(self):
        segment = FIXTURE["idle_afternoon"]
        with tempfile.TemporaryDirectory() as tmp:
            clock = attribution.Clock(datetime.fromisoformat(segment["base"]))
            app = make_app(tmp, clock, door="on")
            self.assertEqual(timer(app), "off")
            states = []
            replay(app, clock, segment, on_event=lambda at: states.append(timer(app)))
            self.assertEqual(set(states), {"off"})

    def test_a_cancelled_timer_goes_and_takes_the_pick_with_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = attribution.Clock(T0)
            app = make_app(tmp, clock)
            for i in range(15 * 6):
                power(app, clock, T0 + timedelta(seconds=10 * i), 0.07 if i % 2 else 0.065)
            self.assertEqual(timer(app), "on")
            pick(app, "Intensive")
            for i in range(12 * 6):
                power(app, clock, T0 + timedelta(minutes=15, seconds=10 * i), 0.016 if i % 2 else 0.018)
            self.assertEqual(timer(app), "off")
            self.assertEqual(app.entities[SELECTOR]["state"], "—")
            self.assertIn((SHORT, "—"), app.selects)

    def test_a_pick_with_no_timer_and_no_finished_run_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = attribution.Clock(T0)
            app = make_app(tmp, clock)
            pick(app, "Intensive")
            self.assertTrue(logged(app, "ignored"))


class SelectorsAroundAnUntimedRun(unittest.TestCase):
    def test_a_start_without_a_timer_resets_the_selectors_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = attribution.Clock(T0)
            app = make_app(tmp, clock, programme="ECO", short="Yes")
            attribution.load_and_start(app, clock, close_at=T0)
            self.assertEqual((app.entities[SELECTOR]["state"], app.entities[SHORT]["state"]), ("—", "—"))
            self.assertEqual(timer(app), "off")

    def test_emptying_resets_a_pick_made_after_the_last_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = attribution.Clock(T0)
            app = make_app(tmp, clock)
            attribution.load_and_start(app, clock, close_at=T0)
            attribution.finish(app, clock)
            pick(app, "Intensive")
            self.assertEqual(app.entities[SELECTOR]["state"], "Intensive")
            clock.now += timedelta(minutes=30)
            attribution.open_door(app)
            clock.now += timedelta(minutes=2)
            attribution.close_door(app)
            self.assertEqual(app.state, "Off")
            self.assertEqual(app.entities[SELECTOR]["state"], "—")


if __name__ == "__main__":
    unittest.main()
