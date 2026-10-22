# tests/test_dishwasher_attribution.py - who started and who emptied the dishwasher. Runs the REAL
# DishwasherMonitor.initialize() against the REAL ActorAttribution (apps/presence) and checks what
# the real house_events.appliance_actor (apps/home_pulse) makes of the published attributes.
# Mirrors test_dryer_attribution.py; the dishwasher's own shape is a door close that is never
# consumed (it clamps start_time and is stored with the cycle), a start that is not re-attributed
# when a false Unemptied reverts to Running, and a peek (door shut within min_emptying_seconds)
# that takes the emptying back out of the entity and the record.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import ast
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "presence"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "home_pulse"))

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

import actor_followup  # noqa: E402
import cycle_persistence  # noqa: E402
import cycle_store as cs  # noqa: E402
import dishwasher_monitor as dm  # noqa: E402
import house_events  # noqa: E402
from test_dryer_attribution import Clock, make_actor  # noqa: E402  (real ActorAttribution on a presence timeline)

STATE_ENTITY = "sensor.dishwasher_state"
UI_SELECT = "input_select.dishwasher_state"
POWER_SENSOR = "sensor.dishwasher_plug_power"
ENERGY_SENSOR = "sensor.dishwasher_plug_energy"
DOOR_SENSOR = "binary_sensor.dishwasher_door_contact"
BUTTON = "input_button.dishwasher_emptied"
REAL_PROGRAMMES = str(Path(__file__).resolve().parents[1] / "dishwasher_programmes.yaml")
T0 = datetime(2026, 10, 14, 17, 0, 0, tzinfo=timezone.utc)
KRISTINE_UID = "5bee0000aaaa4bbbbcccc0000dddd111"


def _entity_get_state(entities):
    def get_state(entity, attribute=None, **kwargs):
        rec = entities.get(entity)
        if rec is None:
            return None
        if attribute == "all":
            return {
                "state": rec.get("state"),
                "attributes": dict(rec.get("attributes") or {}),
                "last_changed": rec.get("last_changed"),
                "last_updated": rec.get("last_changed"),
            }
        if attribute:
            return (rec.get("attributes") or {}).get(attribute)
        return rec.get("state")

    return get_state


def make_app(tmpdir, clock, actor=None, *, sensor_state=None, sensor_attrs=None, helper_state=None,
             power_w="0", energy_kwh="1.7"):
    """Real DishwasherMonitor; only AppDaemon primitives are faked. Every runtime file lives in
    tmpdir, so a second make_app on the same tmpdir is the same box after a reload or an HA restart."""
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
    entities = {
        STATE_ENTITY: {"state": sensor_state, "attributes": dict(sensor_attrs or {}), "last_changed": None},
        UI_SELECT: {"state": helper_state, "attributes": {}, "last_changed": None},
        POWER_SENSOR: {"state": power_w, "attributes": {}, "last_changed": None},
        ENERGY_SENSOR: {"state": energy_kwh, "attributes": {}, "last_changed": None},
        DOOR_SENSOR: {"state": "off", "attributes": {}, "last_changed": None},
    }
    app.entities = entities
    app.get_state = _entity_get_state(entities)
    app._history = {}
    app.history_asked = []

    def get_history(entity_id=None, **kwargs):
        app.history_asked.append(entity_id)
        return [list(app._history.get(entity_id, []))]

    app.get_history = get_history
    app.args = {
        "power_sensor": POWER_SENSOR,
        "energy_sensor": ENERGY_SENSOR,
        "door_sensor": DOOR_SENSOR,
        "state_entity": STATE_ENTITY,
        "ui_state_entity": UI_SELECT,
        "start_w": 8,
        "stop_w": 2,
        "run_for": 60,
        "stop_for": 90,
        "feedback_file": str(Path(tmpdir) / "dishwasher_feedback_test.json"),
        "programmes_file": REAL_PROGRAMMES,
        "state_file": str(Path(tmpdir) / "dishwasher_cycle_state.json"),
    }
    app._now_utc = clock
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_app = lambda name: actor if name == "ActorAttribution" else None
    app.create_task = lambda coro: None
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None
    app.timers = []

    def run_in(cb, delay, **kw):
        app.timers.append((cb, delay, kw))
        return f"timer:{len(app.timers)}:{getattr(cb, '__name__', cb)}"

    app.run_in = run_in

    def set_state(entity_id, **kw):
        rec = entities.setdefault(entity_id, {"state": None, "attributes": {}, "last_changed": None})
        if kw.get("state") is not None:
            rec["state"] = kw["state"]
        if kw.get("attributes") is not None:
            if kw.get("replace"):
                rec["attributes"] = dict(kw["attributes"])
            else:
                rec["attributes"].update(kw["attributes"])
        rec["last_changed"] = clock()

    app.set_state = set_state
    return app


def published(app):
    return app.entities[STATE_ENTITY]["attributes"]


def feedback_cycles(app):
    path = Path(app.feedback_file)
    return json.loads(path.read_text())["cycles"] if path.exists() else []


def stored(tmp):
    return cs.CycleStore(Path(tmp) / "dishwasher_cycle_state.json", "dishwasher").load()


def timers_named(app, name):
    return [(cb, delay, kw) for cb, delay, kw in app.timers if getattr(cb, "__name__", "") == name]


def load_and_start(app, clock, close_at=None):
    """Door close (if any) at close_at, then a hot plug 20 s later - the high-confidence start. Without
    a door close just closed, two readings are needed (high_power_threshold)."""
    if close_at is not None:
        clock.now = close_at
        app.entities[DOOR_SENSOR]["state"] = "off"
        app._door_state_changed(DOOR_SENSOR, None, "on", "off", {})
        clock.now = close_at + timedelta(seconds=20)
    app.entities[POWER_SENSOR]["state"] = "900"
    for _ in range(2):
        if app.state != "Running":
            app._power_changed(POWER_SENSOR, None, None, "900", {})
    assert app.state == "Running", app.state


def finish(app, clock):
    """The finish as _finish_with_dry_tail's immediate branch makes it: Unemptied, then the record."""
    clock.now += timedelta(minutes=240)
    app.last_state_change = clock.now - timedelta(seconds=700)
    app.entities[POWER_SENSOR]["state"] = "0"
    app.entities[ENERGY_SENSOR]["state"] = str(float(app.entities[ENERGY_SENSOR]["state"]) + 1.0)
    app._transition_to_unemptied(skip_announce=True, run_minutes=235.0, energy_used=1.0)
    assert app.state == "Unemptied", app.state
    app._save_cycle_feedback(
        predicted="eco", confirmed="eco", duration_min=235.0, energy_kwh=1.0, max_power_w=1800.0,
    )


def open_door(app):
    app.entities[DOOR_SENSOR]["state"] = "on"
    app._door_state_changed(DOOR_SENSOR, None, "off", "on", {})


def close_door(app):
    app.entities[DOOR_SENSOR]["state"] = "off"
    app._door_state_changed(DOOR_SENSOR, None, "on", "off", {})


def press_pill(app, user_id):
    app._on_emptied_button("state_changed", {
        "entity_id": BUTTON,
        "old_state": {"state": "2026-10-04T14:23:32+00:00"},
        "new_state": {"state": "2026-10-14T20:00:00+00:00", "context": {"user_id": user_id}},
    }, {})


class StartCapturedOnce(unittest.TestCase):

    def test_start_is_attributed_once_at_the_loading_door_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()

            load_and_start(app, clock, close_at=T0)

            self.assertEqual(actor.calls, [("dishwasher_start", T0, None)])
            self.assertEqual(published(app)["started_by"], "mikkel")
            self.assertEqual(published(app)["started_by_method"], "sole_occupant")
            self.assertEqual((stored(tmp)["started_by"], stored(tmp)["started_by_method"]), ("mikkel", "sole_occupant"))

            # A classification tick and a door peek mid-run belong to the same cycle.
            app._update_running_attributes()
            self.assertEqual(published(app)["started_by"], "mikkel")
            clock.now += timedelta(minutes=20)
            open_door(app)
            self.assertEqual(app.state, "Paused")
            clock.now += timedelta(seconds=30)
            close_door(app)
            app._transition_to_running_from_pause()
            self.assertEqual(app.state, "Running")
            finish(app, clock)

            self.assertEqual(len(actor.calls), 1)
            attrs = published(app)
            self.assertEqual((attrs["started_by"], attrs["started_by_method"]), ("mikkel", "sole_occupant"))
            self.assertEqual(house_events.appliance_actor("finished", attrs), "Mikkel")
            record = feedback_cycles(app)[-1]
            self.assertEqual(record["started_by"], "mikkel")
            self.assertEqual(record["attribution"]["start"]["method"], "sole_occupant")
            self.assertNotIn("emptied_by", record)

    def test_the_door_close_not_the_power_edge_is_the_anchor(self):
        # Kristine loads alone; Mikkel is home before the power edge confirms.
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [
                (T0 - timedelta(hours=2), {"kristine"}),
                (T0 + timedelta(seconds=10), {"kristine", "mikkel"}),
            ])
            app = make_app(tmp, clock, actor)
            app.initialize()

            load_and_start(app, clock, close_at=T0)

            self.assertEqual(actor.calls[0][1], T0)
            self.assertEqual(published(app)["started_by"], "kristine")

    def test_a_stale_door_close_falls_back_to_the_power_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=20), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            app.last_door_closed_at = T0
            clock.now = T0 + timedelta(seconds=app.LOAD_ANCHOR_MAX_AGE_S + 60)

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dishwasher_start", clock.now, None)])

    def test_without_a_door_close_the_power_edge_is_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dishwasher_start", T0, None)])

    def test_the_close_in_memory_is_not_consumed_and_the_end_of_the_cycle_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            load_and_start(app, clock, close_at=T0)
            self.assertEqual(app.last_door_closed_at, T0)

            app._transition_to_off("test", force=True)

            self.assertIsNone(app.last_door_closed_at)
            self.assertIsNone(app._cycle_actor)
            self.assertNotIn("started_by", published(app))
            self.assertNotIn("started_by_method", published(app))

    def test_a_false_unemptied_that_reverts_to_running_keeps_its_starter(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            load_and_start(app, clock, close_at=T0)
            finish(app, clock)
            clock.now += timedelta(minutes=2)

            app.entities[POWER_SENSOR]["state"] = "900"
            app._power_changed(POWER_SENSOR, None, None, "900", {})

            self.assertEqual(app.state, "Running")
            self.assertEqual(len(actor.calls), 1)
            self.assertEqual((published(app)["started_by"], published(app)["started_by_method"]), ("mikkel", "sole_occupant"))
            self.assertEqual(feedback_cycles(app), [])


def loading_close(close_at):
    """Door history of a load going in: opened a minute and a half before it shut."""
    return [
        {"state": "on", "last_changed": cs.format_utc(close_at - timedelta(seconds=90))},
        {"state": "off", "last_changed": cs.format_utc(close_at)},
    ]


class StartAnchoredFromHistory(unittest.TestCase):
    """A start with no door close in memory (AppDaemon restarted while the machine sat Off) takes
    the close from the recorder."""

    CLOSE = T0 - timedelta(hours=2, minutes=30)

    def _booted(self, tmp, states):
        clock = Clock(T0)
        actor = make_actor(clock, [
            (self.CLOSE - timedelta(hours=1), {"mikkel"}),
            (T0 - timedelta(minutes=20), {"mikkel", "kristine"}),
        ])
        app = make_app(tmp, clock, actor)
        app.initialize()
        app._history[DOOR_SENSOR] = states
        return app, clock, actor

    def _history_anchor_logged(self, app):
        return any(kw.get("level") == "INFO" and "from history" in a[0] for a, kw in app.log_calls)

    def test_the_recorders_close_is_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._booted(tmp, loading_close(self.CLOSE))

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dishwasher_start", self.CLOSE, None)])
            self.assertEqual((published(app)["started_by"], published(app)["started_by_method"]), ("mikkel", "sole_occupant"))
            self.assertIn(DOOR_SENSOR, app.history_asked)
            self.assertTrue(self._history_anchor_logged(app))

    def test_the_same_start_is_unknown_without_the_recorders_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._booted(tmp, [])

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dishwasher_start", T0, None)])
            self.assertEqual((published(app)["started_by"], published(app)["started_by_method"]), ("", "unknown"))
            self.assertFalse(self._history_anchor_logged(app))

    def test_without_timer_evidence_the_close_in_memory_is_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._booted(tmp, loading_close(self.CLOSE))
            close_at = T0 - timedelta(minutes=5)

            load_and_start(app, clock, close_at=close_at)

            self.assertEqual(actor.calls, [("dishwasher_start", close_at, None)])

    def test_the_close_that_set_the_timer_is_the_anchor_not_a_later_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            idle_close = T0 - timedelta(hours=6)
            timer_close = T0 - timedelta(hours=5)
            later_close = T0 - timedelta(hours=4)
            doors = [
                {"state": "on", "last_changed": cs.format_utc(idle_close - timedelta(minutes=2))},
                {"state": "off", "last_changed": cs.format_utc(idle_close)},
                {"state": "on", "last_changed": cs.format_utc(timer_close - timedelta(minutes=10))},
                {"state": "off", "last_changed": cs.format_utc(timer_close)},
                {"state": "on", "last_changed": cs.format_utc(later_close - timedelta(seconds=20))},
                {"state": "off", "last_changed": cs.format_utc(later_close)},
            ]
            app, clock, actor = self._booted(tmp, doors)
            app._history[POWER_SENSOR] = [
                {"state": "0.02", "last_changed": cs.format_utc(idle_close - timedelta(hours=1))},
                {"state": "0.8", "last_changed": cs.format_utc(timer_close - timedelta(minutes=10))},
                {"state": "0.07", "last_changed": cs.format_utc(timer_close + timedelta(minutes=1))},
                {"state": "0.9", "last_changed": cs.format_utc(later_close - timedelta(seconds=20))},
                {"state": "0.07", "last_changed": cs.format_utc(later_close + timedelta(minutes=1))},
            ]

            load_and_start(app, clock, close_at=later_close)

            self.assertEqual(actor.calls, [("dishwasher_start", timer_close, None)])

    def test_a_failing_lookup_falls_back_to_the_power_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor = self._booted(tmp, [])

            def broken(entity_id=None, **kwargs):
                raise RuntimeError("recorder unavailable")

            app.get_history = broken
            load_and_start(app, clock)

            self.assertEqual(app.state, "Running")
            self.assertEqual(actor.calls, [("dishwasher_start", T0, None)])

    def test_a_close_over_the_anchor_bound_or_a_contact_reconnect_is_not_used(self):
        old = loading_close(T0 - timedelta(seconds=dm.DishwasherMonitor.LOAD_ANCHOR_MAX_AGE_S + 60))
        reconnect = [
            {"state": "unavailable", "last_changed": cs.format_utc(self.CLOSE - timedelta(minutes=1))},
            {"state": "off", "last_changed": cs.format_utc(self.CLOSE)},
        ]
        for name, states in (("too old", old), ("reconnect", reconnect)):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                app, clock, actor = self._booted(tmp, states)

                load_and_start(app, clock)

                self.assertEqual(actor.calls, [("dishwasher_start", T0, None)])


class EmptiedAttribution(unittest.TestCase):

    def _unemptied(self, tmp, timeline, user_ids=None):
        clock = Clock(T0)
        actor = make_actor(clock, timeline, user_ids)
        app = make_app(tmp, clock, actor)
        app.initialize()
        load_and_start(app, clock, close_at=T0)
        finish(app, clock)
        clock.now += timedelta(minutes=30)
        return app, actor, clock

    def test_emptied_by_the_pill_user_even_with_several_people_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, _clock = self._unemptied(
                tmp, [(T0 - timedelta(hours=2), {"mikkel", "kristine"})], {KRISTINE_UID: "kristine"},
            )

            press_pill(app, KRISTINE_UID)

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(actor.calls[-1], ("dishwasher_emptied", None, KRISTINE_UID))
            attrs = published(app)
            self.assertEqual((attrs["emptied_by"], attrs["emptied_by_method"]), ("kristine", "dashboard_action"))
            self.assertEqual(house_events.appliance_actor("emptied", attrs), "Kristine")
            record = feedback_cycles(app)[-1]
            self.assertEqual(record["emptied_by"], "kristine")
            self.assertTrue(record["emptied_ts"])
            self.assertEqual(record["attribution"]["empty"]["method"], "dashboard_action")
            self.assertIn("start", record["attribution"])

    def test_emptied_by_the_sole_occupant_when_the_door_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, _clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])

            open_door(app)

            self.assertEqual(app.state, "Emptied")
            attrs = published(app)
            self.assertEqual((attrs["emptied_by"], attrs["emptied_by_method"]), ("claudia", "sole_occupant"))
            self.assertEqual((attrs["started_by"], attrs["started_by_method"]), ("claudia", "sole_occupant"))
            self.assertEqual(house_events.appliance_actor("emptied", attrs), "Claudia")
            self.assertEqual(feedback_cycles(app)[-1]["emptied_by"], "claudia")

    def test_the_door_opened_first_path_names_the_emptier_on_the_record_it_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"claudia"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            load_and_start(app, clock, close_at=T0)
            clock.now += timedelta(minutes=300)
            app.start_time = clock.now - timedelta(minutes=300)
            app.last_state_change = clock.now - timedelta(seconds=700)
            app.entities[POWER_SENSOR]["state"] = "0"
            app.entities[ENERGY_SENSOR]["state"] = "2.7"

            open_door(app)

            self.assertEqual(app.state, "Emptied")
            record, = feedback_cycles(app)
            self.assertEqual((record["started_by"], record["emptied_by"]), ("claudia", "claudia"))
            self.assertEqual(record["end_reason"], "door_opened_first")
            self.assertTrue(record["emptied_ts"])

    def test_unknown_when_several_people_are_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, _clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"mikkel", "kristine"})])
            finished = dict(published(app))

            open_door(app)

            attrs = published(app)
            self.assertEqual((finished["started_by"], finished["started_by_method"]), ("", "unknown"))
            self.assertEqual((attrs["emptied_by"], attrs["emptied_by_method"]), ("", "unknown"))
            self.assertIsNone(house_events.appliance_actor("finished", finished))
            self.assertIsNone(house_events.appliance_actor("emptied", attrs))
            record = feedback_cycles(app)[-1]
            self.assertIsNone(record["started_by"])
            self.assertIsNone(record["emptied_by"])
            self.assertEqual(record["attribution"]["empty"]["reason"], "multi_occupant")

    def test_a_second_emptying_never_restamps_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])
            open_door(app)
            first = dict(feedback_cycles(app)[-1])

            clock.now += timedelta(minutes=5)
            app._mark_last_cycle_emptied({"person": "mikkel", "method": "sole_occupant", "anchor": "x"})

            self.assertEqual(feedback_cycles(app)[-1], first)

    def test_a_forced_emptying_from_paused_leaves_the_previous_runs_record_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])
            open_door(app)
            before = dict(feedback_cycles(app)[-1])
            clock.now += timedelta(minutes=2)
            close_door(app)
            self.assertEqual(app.state, "Off")
            clock.now += timedelta(minutes=10)
            app.state = "Paused"
            app.entities[STATE_ENTITY]["state"] = "Paused"

            press_pill(app, "unmapped-user")

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(feedback_cycles(app)[-1], before)

    def test_an_emptying_found_after_an_outage_names_nobody_but_is_still_dated(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, _clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])
            calls_before = len(actor.calls)

            app._transition_to_emptied("Door opened during the outage - already emptied", observed=False)

            self.assertEqual(len(actor.calls), calls_before)
            attrs = published(app)
            self.assertEqual((attrs["emptied_by"], attrs["emptied_by_method"]), ("", "unknown"))
            self.assertIsNone(house_events.appliance_actor("emptied", attrs))
            record = feedback_cycles(app)[-1]
            self.assertTrue(record["emptied_ts"])
            self.assertNotIn("emptied_by", record)
            self.assertNotIn("empty", record["attribution"])
            self.assertEqual(app.timers and timers_named(app, "_actor_followup_empty"), [])

    def test_a_door_opening_found_by_the_poll_names_nobody(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])
            calls_before = len(actor.calls)
            app._history[DOOR_SENSOR] = [
                {"state": "on", "last_changed": cs.format_utc(clock.now - timedelta(minutes=10))},
            ]

            app._poll_power({})

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(len(actor.calls), calls_before)
            self.assertEqual((published(app)["emptied_by"], published(app)["emptied_by_method"]), ("", "unknown"))
            self.assertNotIn("emptied_by", feedback_cycles(app)[-1])


class PeekTakesTheEmptyingBack(unittest.TestCase):
    """A door shut within min_emptying_seconds is a peek: back to Unemptied, and the name that
    was written for the emptying goes with it."""

    def _emptied(self, tmp, timeline):
        clock = Clock(T0)
        actor = make_actor(clock, timeline)
        app = make_app(tmp, clock, actor)
        app.initialize()
        load_and_start(app, clock, close_at=T0)
        finish(app, clock)
        clock.now += timedelta(minutes=30)
        open_door(app)
        assert app.state == "Emptied"
        return app, actor, clock

    def test_the_record_and_the_entity_lose_the_name_and_the_next_real_emptying_regains_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, clock = self._emptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])
            self.assertEqual(published(app)["emptied_by"], "claudia")
            self.assertEqual(feedback_cycles(app)[-1]["emptied_by"], "claudia")

            clock.now += timedelta(seconds=10)
            close_door(app)

            self.assertEqual(app.state, "Unemptied")
            record = feedback_cycles(app)[-1]
            for key in ("emptied_ts", "emptied_by"):
                self.assertNotIn(key, record)
            self.assertNotIn("empty", record["attribution"])
            self.assertEqual(record["attribution"]["start"]["person"], "claudia")
            self.assertNotIn("emptied_by", published(app))
            self.assertNotIn("emptied_by_method", published(app))
            self.assertEqual(published(app)["started_by"], "claudia")

            clock.now += timedelta(minutes=2)
            open_door(app)

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(published(app)["emptied_by"], "claudia")
            self.assertEqual(feedback_cycles(app)[-1]["emptied_by"], "claudia")
            self.assertTrue(feedback_cycles(app)[-1]["emptied_ts"])

    def test_a_record_that_was_never_attributed_survives_the_revert(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, clock = self._emptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])
            path = Path(app.feedback_file)
            data = json.loads(path.read_text())
            del data["cycles"][-1]["attribution"]
            path.write_text(json.dumps(data))

            clock.now += timedelta(seconds=10)
            close_door(app)

            self.assertEqual(app.state, "Unemptied")
            self.assertNotIn("emptied_ts", feedback_cycles(app)[-1])


class ReloadRestore(unittest.TestCase):

    def test_starter_survives_an_appdaemon_reload_mid_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"claudia"})])
            first = make_app(tmp, clock, actor)
            first.initialize()
            load_and_start(first, clock, close_at=T0)

            # Same entity (AD-only reload), but the room has filled up since the start.
            actor._snapshot_log += (type(actor._snapshot_log[0])(clock.now, frozenset({"claudia", "mikkel"}), frozenset()),)
            clock.now += timedelta(minutes=5)
            second = make_app(tmp, clock, actor, sensor_state="Running",
                              sensor_attrs=published(first), power_w="900", energy_kwh="1.7")
            second.initialize()

            self.assertEqual(len(actor.calls), 1)
            self.assertEqual(second._cycle_actor["person"], "claudia")
            self.assertEqual(published(second)["started_by"], "claudia")
            finish(second, clock)
            self.assertEqual(house_events.appliance_actor("finished", published(second)), "Claudia")
            self.assertEqual(feedback_cycles(second)[-1]["started_by"], "claudia")

    def test_starter_survives_an_ha_restart_through_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"kristine"})])
            first = make_app(tmp, clock, actor)
            first.initialize()
            load_and_start(first, clock, close_at=T0)
            self.assertEqual((stored(tmp)["started_by"], stored(tmp)["started_by_method"]), ("kristine", "sole_occupant"))

            clock.now += timedelta(minutes=5)
            second = make_app(tmp, clock, actor, sensor_state=None, power_w="900")  # HA erased the entity
            second.initialize()

            self.assertEqual(second.state, "Running")
            self.assertEqual(published(second)["started_by"], "kristine")
            self.assertEqual(published(second)["started_by_method"], "sole_occupant")

    def test_starter_survives_a_reload_while_unemptied_for_the_emptied_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            first = make_app(tmp, clock, actor)
            first.initialize()
            load_and_start(first, clock, close_at=T0)
            finish(first, clock)

            clock.now += timedelta(hours=3)
            second = make_app(tmp, clock, actor, sensor_state="Unemptied", sensor_attrs=published(first))
            second.initialize()
            open_door(second)

            attrs = published(second)
            self.assertEqual((attrs["started_by"], attrs["emptied_by"]), ("mikkel", "mikkel"))
            record = feedback_cycles(second)[-1]
            self.assertEqual((record["started_by"], record["emptied_by"]), ("mikkel", "mikkel"))

    def test_a_cycle_without_a_recorded_starter_restores_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            app = make_app(tmp, clock, sensor_state="Running", power_w="900", sensor_attrs={
                "cycle_start_time": cs.format_utc(T0 - timedelta(minutes=30)),
                "energy_at_start": 1.0,
            })
            app.initialize()

            self.assertEqual(app.state, "Running")
            self.assertIsNone(app._cycle_actor["person"])
            self.assertEqual((published(app)["started_by"], published(app)["started_by_method"]), ("", "unknown"))

    def test_a_starter_from_a_rejected_store_is_not_revived(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=30), {"mikkel"})])
            first = make_app(tmp, clock, actor)
            first.initialize()
            load_and_start(first, clock, close_at=T0)

            clock.now += timedelta(hours=20)  # the stored Running is far past max_running_hours
            second = make_app(tmp, clock, actor, sensor_state=None, helper_state="Unemptied")
            second.initialize()

            self.assertEqual(second.state, "Unemptied")
            self.assertIsNone(second._cycle_actor)
            self.assertNotIn("started_by", published(second))


class BootSelfHealAfterOutage(unittest.TestCase):
    """A cycle that finished and was emptied while AppDaemon was down: the starter is restored
    from the store, nobody is named as emptier, and the record is dated but not attributed."""

    def test_outage_emptying_names_nobody(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=8), {"kristine"})])
            app = make_app(tmp, clock, actor, sensor_state=None, helper_state="Running", power_w="0", energy_kwh="1.7")
            cs.CycleStore(Path(tmp) / "dishwasher_cycle_state.json", "dishwasher").save({
                "state": "Running",
                "cycle_start_time": cs.format_utc(T0 - timedelta(minutes=200)),
                "energy_at_start": "1.0",
                "detected_programme": "gentle",
                "expected_dur_at_start": 149,
                "max_power_w": 1800.0,
                "last_high_power_time": cs.format_utc(T0 - timedelta(minutes=55)),
                "notification_sent": False,
                "started_by": "mikkel",
                "started_by_method": "sole_occupant",
            })
            app._history[DOOR_SENSOR] = [{"state": "on", "last_changed": cs.format_utc(T0 - timedelta(minutes=40))}]

            app.initialize()

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(actor.calls, [])
            attrs = published(app)
            self.assertEqual((attrs["started_by"], attrs["emptied_by"], attrs["emptied_by_method"]), ("mikkel", "", "unknown"))
            self.assertIsNone(house_events.appliance_actor("emptied", attrs))
            record, = feedback_cycles(app)
            self.assertEqual(record["started_by"], "mikkel")
            self.assertTrue(record["emptied_ts"])
            self.assertNotIn("emptied_by", record)


class PublishedAttributesFollowTheCycle(unittest.TestCase):

    def test_the_starter_is_on_every_state_of_the_run_and_off_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            seen = {}

            load_and_start(app, clock, close_at=T0)
            seen["Running"] = dict(published(app))
            finish(app, clock)
            seen["Unemptied"] = dict(published(app))
            open_door(app)
            seen["Emptied"] = dict(published(app))
            clock.now += timedelta(minutes=2)
            close_door(app)
            seen["Off"] = dict(published(app))

            self.assertEqual(app.state, "Off")
            for state in ("Running", "Unemptied", "Emptied"):
                self.assertEqual(
                    (seen[state]["started_by"], seen[state]["started_by_method"]), ("mikkel", "sole_occupant"), state,
                )
            self.assertNotIn("started_by", seen["Off"])
            self.assertNotIn("started_by_method", seen["Off"])

    def test_a_new_unemptied_never_carries_the_previous_emptier(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            load_and_start(app, clock, close_at=T0)
            finish(app, clock)
            open_door(app)
            clock.now += timedelta(minutes=2)
            close_door(app)
            self.assertEqual(app.state, "Off")
            self.assertEqual(published(app)["emptied_by"], "mikkel")

            clock.now += timedelta(minutes=30)
            app.last_state_change = clock.now - timedelta(seconds=700)
            load_and_start(app, clock, close_at=clock.now)
            finish(app, clock)

            self.assertNotIn("emptied_by", published(app))
            self.assertNotIn("emptied_by_method", published(app))


class NoAppDaemonInternalShadowing(unittest.TestCase):
    """An app attribute named like an ADAPI member (`_log` above all: ADAPI.log calls self._log)
    breaks every self.log() at runtime, which unit tests cannot see because they stub log."""

    RESERVED = {"_log", "log", "error", "args", "name", "AD", "logger", "set_state",
                "get_state", "listen_state", "listen_event", "run_in", "run_every",
                "get_history", "create_task", "call_service", "get_app"}

    def test_no_class_attribute_shadows_an_adapi_member(self):
        offenders = [
            name
            for cls in (dm.DishwasherMonitor, cycle_persistence.CyclePersistenceMixin, actor_followup.ActorFollowupMixin)
            for name, value in vars(cls).items()
            if name in self.RESERVED and not callable(value)
        ]
        self.assertEqual(offenders, [])

    def test_no_instance_attribute_assigned_in_source_shadows_an_adapi_member(self):
        assigned = set()
        for module in (dm, cycle_persistence, actor_followup):
            for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    if isinstance(node.value, ast.Name) and node.value.id == "self":
                        assigned.add(node.attr)
        self.assertEqual(sorted(assigned & self.RESERVED), [])


if __name__ == "__main__":
    unittest.main()
