# tests/test_dryer_attribution.py - who started and who emptied the dryer. Runs the REAL
# DryerMonitor.initialize() against the REAL ActorAttribution (apps/presence) and checks what the
# real house_events.appliance_actor (apps/home_pulse) makes of the published attributes.
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

import actor_attribution as aa  # noqa: E402
import actor_followup  # noqa: E402
import cycle_persistence  # noqa: E402
import cycle_store as cs  # noqa: E402
import dryer_monitor as dm  # noqa: E402
import house_events  # noqa: E402

STATE_ENTITY = "sensor.dryer_state"
UI_SELECT = "input_select.dryer_state"
POWER_SENSOR = "sensor.dryer_plug_power"
ENERGY_SENSOR = "sensor.dryer_plug_energy"
DOOR_SENSOR = "binary_sensor.dryer_door_contact"
BUTTON = "input_button.dryer_emptied"
T0 = datetime(2026, 10, 15, 16, 0, 0, tzinfo=timezone.utc)
KRISTINE_UID = "5bee0000aaaa4bbbbcccc0000dddd111"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make_actor(clock, timeline, user_ids=None):
    """Real ActorAttribution fed a presence timeline [(ts, {home people})] instead of HA history."""
    app = aa.ActorAttribution.__new__(aa.ActorAttribution)
    app.person_entities = ["person.mikkel", "person.kristine", "person.claudia"]
    app.door_entity = "binary_sensor.apartment_door_open"
    app.publish_sensor = "sensor.household_actor"
    app.user_id_overrides = {}
    app._person_state = {}
    app._door_state = "off"
    app._door_events = ()
    app._snapshot_log = tuple(aa._Snapshot(ts, frozenset(home), frozenset()) for ts, home in timeline)
    app._user_id_to_person = dict(user_ids or {})
    app._now_utc = clock
    app.log = lambda *a, **kw: None
    app.calls = []
    real_attribute = app.attribute

    def spy(event, at=None, user_id=None):
        app.calls.append((event, at, user_id))
        return real_attribute(event, at=at, user_id=user_id)

    app.attribute = spy
    return app


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


def make_app(tmpdir, clock, actor=None, *, sensor_state=None, sensor_attrs=None, power_w="0", energy_kwh="10.0"):
    """Real DryerMonitor; only AppDaemon primitives are faked. Every runtime file lives in tmpdir,
    so a second make_app on the same tmpdir is the same box after a reload or an HA restart."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)
    entities = {
        STATE_ENTITY: {"state": sensor_state, "attributes": dict(sensor_attrs or {}), "last_changed": None},
        UI_SELECT: {"state": None, "attributes": {}, "last_changed": None},
        POWER_SENSOR: {"state": power_w, "attributes": {}, "last_changed": None},
        ENERGY_SENSOR: {"state": energy_kwh, "attributes": {}, "last_changed": None},
        DOOR_SENSOR: {"state": "off", "attributes": {}, "last_changed": None},
    }
    app.entities = entities
    app.get_state = _entity_get_state(entities)
    app.get_history = lambda **kwargs: []
    app.args = {
        "power_sensor": POWER_SENSOR,
        "energy_sensor": ENERGY_SENSOR,
        "door_sensor": DOOR_SENSOR,
        "state_entity": STATE_ENTITY,
        "ui_state_entity": UI_SELECT,
        "start_w": 8,
        "stop_w": 5,
        "run_for": 60,
        "stop_for": 60,
        "feedback_file": str(Path(tmpdir) / "dryer_feedback_test.json"),
        "programmes_file": str(Path(tmpdir) / "dryer_programmes_test.yaml"),
        "state_file": str(Path(tmpdir) / "dryer_cycle_state.json"),
    }
    app._now_utc = clock
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_app = lambda name: actor if name == "ActorAttribution" else None
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None
    app.run_in = lambda cb, delay, **kw: f"timer:{getattr(cb, '__name__', cb)}"
    app.run_every = lambda cb, start, interval, **kw: f"every:{getattr(cb, '__name__', cb)}"
    app.datetime = clock

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


def load_and_start(app, clock, close_at=None):
    """Door close (if any) at close_at, then the power edge confirmed 20 s later."""
    if close_at is not None:
        clock.now = close_at
        app._door_state_changed(DOOR_SENSOR, None, "on", "off", {})
        clock.now = close_at + timedelta(seconds=20)
    app.entities[POWER_SENSOR]["state"] = "900"
    app._confirm_running({})


def finish(app, clock):
    """Drive the real low-power finish: past the 80% guard, out of cooling, 1 kWh used."""
    clock.now += timedelta(minutes=100)
    app.start_time = clock.now - timedelta(minutes=100)
    app.last_state_change = clock.now - timedelta(seconds=700)
    app.entities[POWER_SENSOR]["state"] = "0"
    app.entities[ENERGY_SENSOR]["state"] = str(float(app.entities[ENERGY_SENSOR]["state"]) + 1.0)
    app._confirm_finished({})
    assert app.state == "Unemptied", app.state


def press_pill(app, user_id):
    app._on_emptied_button("state_changed", {
        "entity_id": BUTTON,
        "old_state": {"state": "2026-10-04T14:23:32+00:00"},
        "new_state": {"state": "2026-10-15T18:00:00+00:00", "context": {"user_id": user_id}},
    }, {})


class StartCapturedOnce(unittest.TestCase):

    def test_start_is_attributed_once_at_the_loading_door_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()

            load_and_start(app, clock, close_at=T0)

            self.assertEqual(actor.calls, [("dryer_start", T0, None)])
            self.assertEqual(published(app)["started_by"], "mikkel")
            self.assertEqual(published(app)["started_by_method"], "sole_occupant")

            # Classification ticks and a pause/resume belong to the same cycle.
            app._update_running_attributes()
            clock.now += timedelta(minutes=20)
            app._door_state_changed(DOOR_SENSOR, None, "off", "on", {})
            self.assertEqual(app.state, "Paused")
            app._door_state_changed(DOOR_SENSOR, None, "on", "off", {})
            self.assertEqual(app.state, "Running")
            finish(app, clock)

            self.assertEqual(len(actor.calls), 1)
            attrs = published(app)
            self.assertEqual((attrs["started_by"], attrs["started_by_method"]), ("mikkel", "sole_occupant"))
            self.assertEqual(house_events.appliance_actor("finished", attrs), "Mikkel")
            record = feedback_cycles(app)[-1]
            self.assertEqual(record["started_by"], "mikkel")
            self.assertEqual(record["attribution"]["start"]["method"], "sole_occupant")

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

    def test_stale_door_close_falls_back_to_the_power_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=20), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            app._door_state_changed(DOOR_SENSOR, None, "on", "off", {})
            clock.now = T0 + timedelta(seconds=app.LOAD_ANCHOR_MAX_AGE_S + 60)

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dryer_start", clock.now, None)])

    def test_without_a_door_close_the_power_edge_is_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dryer_start", T0, None)])

    def test_a_contact_reconnect_is_not_a_door_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            app = make_app(tmp, clock)
            app.initialize()
            app._door_state_changed(DOOR_SENSOR, None, "unavailable", "off", {})
            self.assertIsNone(app.last_door_closed_at)

    def test_one_door_close_anchors_one_cycle_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=2), {"mikkel"})])
            app = make_app(tmp, clock, actor)
            app.initialize()
            load_and_start(app, clock, close_at=T0)
            app._transition_to_off("test", force=True)
            clock.now += timedelta(minutes=30)
            app.last_state_change = clock.now - timedelta(seconds=700)

            load_and_start(app, clock)

            self.assertEqual(actor.calls[-1], ("dryer_start", clock.now, None))


def loading_close(close_at):
    """Door history of a load going in: opened a minute and a half before it shut."""
    return [
        {"state": "on", "last_changed": cs.format_utc(close_at - timedelta(seconds=90))},
        {"state": "off", "last_changed": cs.format_utc(close_at)},
    ]


def serve_door_history(app, states):
    """The recorder holds `states` for the door sensor, or raises when handed an exception.
    Returns the list of entities it was asked about."""
    asked = []

    def get_history(entity_id=None, **kwargs):
        asked.append(entity_id)
        if isinstance(states, Exception):
            raise states
        return [list(states if entity_id == DOOR_SENSOR else [])]

    app.get_history = get_history
    return asked


class StartAnchoredFromHistory(unittest.TestCase):
    """A start with no door close in memory (AppDaemon restarted after the door shut on a
    delayed-start load) takes the close from the recorder."""

    CLOSE = T0 - timedelta(hours=2, minutes=30)

    def _booted(self, tmp, states):
        clock = Clock(T0)
        actor = make_actor(clock, [
            (self.CLOSE - timedelta(hours=1), {"mikkel"}),
            (T0 - timedelta(minutes=20), {"mikkel", "kristine"}),
        ])
        app = make_app(tmp, clock, actor)
        app.initialize()
        return app, clock, actor, serve_door_history(app, states)

    def _history_anchor_logged(self, app):
        return any(kw.get("level") == "INFO" and "from history" in a[0] for a, kw in app.log_calls)

    def test_the_recorders_close_is_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor, asked = self._booted(tmp, loading_close(self.CLOSE))

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dryer_start", self.CLOSE, None)])
            self.assertEqual((published(app)["started_by"], published(app)["started_by_method"]), ("mikkel", "sole_occupant"))
            self.assertIn(DOOR_SENSOR, asked)
            self.assertTrue(self._history_anchor_logged(app))

    def test_the_same_start_is_unknown_without_the_recorders_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor, _asked = self._booted(tmp, [])

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dryer_start", T0, None)])
            self.assertEqual((published(app)["started_by"], published(app)["started_by_method"]), ("", "unknown"))
            self.assertFalse(self._history_anchor_logged(app))

    def test_a_close_in_memory_never_asks_the_recorder(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor, asked = self._booted(tmp, loading_close(self.CLOSE))
            close_at = T0 - timedelta(minutes=5)

            load_and_start(app, clock, close_at=close_at)

            self.assertEqual(actor.calls, [("dryer_start", close_at, None)])
            self.assertNotIn(DOOR_SENSOR, asked)
            self.assertFalse(self._history_anchor_logged(app))

    def test_a_failing_lookup_falls_back_to_the_power_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor, asked = self._booted(tmp, RuntimeError("recorder unavailable"))

            load_and_start(app, clock)

            self.assertEqual(app.state, "Running")
            self.assertEqual(actor.calls, [("dryer_start", T0, None)])
            self.assertIn(DOOR_SENSOR, asked)
            self.assertTrue(any(
                kw.get("level") == "DEBUG" and "Door-close history lookup failed" in a[0] for a, kw in app.log_calls
            ))

    def test_a_close_over_the_anchor_bound_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, clock, actor, _asked = self._booted(
                tmp, loading_close(T0 - timedelta(seconds=dm.DryerMonitor.LOAD_ANCHOR_MAX_AGE_S + 60)),
            )

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dryer_start", T0, None)])

    def test_a_contact_reconnect_in_the_recorder_is_not_a_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconnect = [
                {"state": "unavailable", "last_changed": cs.format_utc(self.CLOSE - timedelta(minutes=1))},
                {"state": "off", "last_changed": cs.format_utc(self.CLOSE)},
            ]
            app, clock, actor, _asked = self._booted(tmp, reconnect)

            load_and_start(app, clock)

            self.assertEqual(actor.calls, [("dryer_start", T0, None)])


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
            self.assertEqual(actor.calls[-1], ("dryer_emptied", None, KRISTINE_UID))
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

            app._door_state_changed(DOOR_SENSOR, None, "off", "on", {})

            self.assertEqual(app.state, "Emptied")
            attrs = published(app)
            self.assertEqual((attrs["emptied_by"], attrs["emptied_by_method"]), ("claudia", "sole_occupant"))
            self.assertEqual((attrs["started_by"], attrs["started_by_method"]), ("claudia", "sole_occupant"))
            self.assertEqual(house_events.appliance_actor("emptied", attrs), "Claudia")
            self.assertEqual(feedback_cycles(app)[-1]["emptied_by"], "claudia")

    def test_unknown_when_several_people_are_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, _clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"mikkel", "kristine"})])
            finished = dict(published(app))

            app._door_state_changed(DOOR_SENSOR, None, "off", "on", {})

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
            app._door_state_changed(DOOR_SENSOR, None, "off", "on", {})
            first = feedback_cycles(app)[-1]["emptied_ts"]

            clock.now += timedelta(minutes=5)
            app._patch_cycle_record(app._last_saved_record_ts, {"emptied_by": "mikkel", "emptied_ts": "later"})

            record = feedback_cycles(app)[-1]
            self.assertEqual((record["emptied_by"], record["emptied_ts"]), ("claudia", first))

    def test_an_emptying_found_after_an_outage_names_nobody_and_patches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, actor, _clock = self._unemptied(tmp, [(T0 - timedelta(hours=2), {"claudia"})])
            calls_before = len(actor.calls)

            app._transition_to_emptied("Door opened during the outage - already emptied", observed=False)

            self.assertEqual(len(actor.calls), calls_before)
            attrs = published(app)
            self.assertEqual((attrs["emptied_by"], attrs["emptied_by_method"]), ("", "unknown"))
            self.assertIsNone(house_events.appliance_actor("emptied", attrs))
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
            actor._snapshot_log += (aa._Snapshot(clock.now, frozenset({"claudia", "mikkel"}), frozenset()),)
            clock.now += timedelta(minutes=5)
            second = make_app(tmp, clock, actor, sensor_state="Running",
                              sensor_attrs=published(first), power_w="900", energy_kwh="10.0")
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
            stored = cs.CycleStore(Path(tmp) / "dryer_cycle_state.json", "dryer").load()
            self.assertEqual((stored["started_by"], stored["started_by_method"]), ("kristine", "sole_occupant"))

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
            self.assertIsNone(second._last_saved_record_ts)
            second._door_state_changed(DOOR_SENSOR, None, "off", "on", {})

            attrs = published(second)
            self.assertEqual((attrs["started_by"], attrs["emptied_by"]), ("mikkel", "mikkel"))
            record = feedback_cycles(second)[-1]
            self.assertEqual((record["started_by"], record["emptied_by"]), ("mikkel", "mikkel"))

    def test_a_cycle_without_a_recorded_starter_restores_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            app = make_app(tmp, clock, sensor_state="Running", power_w="900", sensor_attrs={
                "cycle_start_time": cs.format_utc(T0 - timedelta(minutes=30)),
                "energy_at_start": 9.5,
            })
            app.initialize()

            self.assertIsNone(app._cycle_actor["person"])
            self.assertEqual((published(app)["started_by"], published(app)["started_by_method"]), ("", "unknown"))


class BootSelfHealAfterOutage(unittest.TestCase):
    """A cycle that finished and was emptied while AppDaemon was down: the starter is restored
    from the store, nobody is named as emptier, and no feedback record is patched."""

    def test_outage_emptying_names_nobody_and_leaves_older_records_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            actor = make_actor(clock, [(T0 - timedelta(hours=8), {"kristine"})])
            app = make_app(tmp, clock, actor, sensor_state=None, power_w="0")
            Path(app.args["feedback_file"]).write_text(json.dumps({"version": 2, "cycles": [
                {"ts": "2026-10-14T10:00:00+02:00", "duration_min": 90.0, "energy_kwh": 1.0,
                 "predicted": "unknown", "confirmed": "unknown", "programme_confirmed_by_human": False,
                 "max_power_w": 850.0},
            ]}))
            cs.CycleStore(Path(tmp) / "dryer_cycle_state.json", "dryer").save({
                "state": "Running",
                "cycle_start_time": cs.format_utc(T0 - timedelta(minutes=200)),
                "energy_at_start": "9.0",
                "detected_programme": "unknown",
                "programme_duration_min": 120,
                "max_power_w": 850.0,
                "started_by": "mikkel",
                "started_by_method": "sole_occupant",
            })
            jump_end = T0 - timedelta(minutes=182)
            history = {
                ENERGY_SENSOR: [
                    {"state": "9.000", "last_changed": cs.format_utc(jump_end - timedelta(seconds=60))},
                    {"state": "9.050", "last_changed": cs.format_utc(jump_end)},
                ],
                DOOR_SENSOR: [{"state": "on", "last_changed": cs.format_utc(T0 - timedelta(minutes=150))}],
            }
            app.get_history = lambda entity_id=None, **kw: [list(history.get(entity_id, []))]

            app.initialize()

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(actor.calls, [])
            attrs = published(app)
            self.assertEqual((attrs["started_by"], attrs["emptied_by"], attrs["emptied_by_method"]), ("mikkel", "", "unknown"))
            self.assertIsNone(house_events.appliance_actor("emptied", attrs))
            older, newest = feedback_cycles(app)
            self.assertNotIn("emptied_by", older)
            self.assertEqual(newest["started_by"], "mikkel")
            self.assertNotIn("emptied_ts", newest)


class NoAppDaemonInternalShadowing(unittest.TestCase):
    """An app attribute named like an ADAPI member (`_log` above all: ADAPI.log calls self._log)
    breaks every self.log() at runtime, which unit tests cannot see because they stub log."""

    RESERVED = {"_log", "log", "error", "args", "name", "AD", "logger", "set_state",
                "get_state", "listen_state", "listen_event", "run_in", "run_every",
                "get_history", "create_task", "call_service", "get_app"}

    def test_no_class_attribute_shadows_an_adapi_member(self):
        offenders = [
            name
            for cls in (dm.DryerMonitor, cycle_persistence.CyclePersistenceMixin, actor_followup.ActorFollowupMixin)
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
