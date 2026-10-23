"""One completion contract exercised through all three real monitor hosts.

Only AppDaemon primitives are faked by the existing initialization fixtures. The
finish, emptying, durable restore, actor patch, and ledger writes run for real.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import test_dishwasher_attribution as dishwasher
import test_dryer_attribution as dryer
from test_washer_restart_survival import make_full_init_app


APPLIANCES = ("washer", "dryer", "dishwasher")
MEASUREMENTS = ("run_time_minutes", "energy_used", "end_reason", "idle_min")


def published(app):
    return app.get_state(app.state_entity, attribute="all")["attributes"]


def measurements(app):
    attrs = published(app)
    return {key: attrs[key] for key in MEASUREMENTS if key in attrs}


def set_sensor(app, entity, state):
    if hasattr(app, "entities"):
        app.entities[entity]["state"] = state
    else:
        app.states[entity] = state


def door(app, opened):
    old, new = ("off", "on") if opened else ("on", "off")
    set_sensor(app, app.door_sensor, new)
    app._door_state_changed(app.door_sensor, None, old, new, {})


class CompletedMeasurements(unittest.TestCase):
    def make_app(self, name, tmp, clock, *, attrs=None, previous=None, erased=False):
        state = "Unemptied" if attrs is not None and not erased else None
        if name == "washer":
            app = make_full_init_app(
                now=clock(), sensor_state=state, sensor_attrs=attrs if not erased else None,
                store_payload=previous._cycle_store.load() if previous else None,
                extra_args={"feedback_file": str(Path(tmp) / "washer_feedback_test.json")},
            )
            self.addCleanup(shutil.rmtree, Path(app.args["state_file"]).parent)
            app._now_utc = clock
            set_sensor(app, app.door_sensor, "off")
        else:
            fixture = dryer if name == "dryer" else dishwasher
            app = fixture.make_app(tmp, clock, sensor_state=state,
                                   sensor_attrs=attrs if not erased else None)
            app.initialize()
        return app

    def started(self, name, tmp, clock):
        app = self.make_app(name, tmp, clock)
        set_sensor(app, app.energy_sensor, "10.0")
        if name == "washer":
            app._begin_running_cycle()
        else:
            (dryer if name == "dryer" else dishwasher).load_and_start(app, clock)
        self.assertEqual(app.state, "Running")
        return app

    def finish(self, name, app, clock):
        if name == "washer":
            clock.now += timedelta(minutes=160)
            set_sensor(app, app.energy_sensor, "11.0")
            app._pending_end_reason = "standby"
            app._transition_to_unemptied(skip_announce=True, force=True)
        else:
            (dryer if name == "dryer" else dishwasher).finish(app, clock)
        self.assertEqual(app.state, "Unemptied")
        self.assertGreater(published(app)["run_time_minutes"], 0)
        self.assertEqual(published(app)["energy_used"], 1.0)
        self.assertEqual(len(dryer.feedback_cycles(app)), 1)
        return app

    def finished(self, name, tmp, clock):
        return self.finish(name, self.started(name, tmp, clock), clock)

    def assert_ledger_measurements(self, app, before):
        cycles = dryer.feedback_cycles(app)
        self.assertEqual(len(cycles), 1)
        # Emptying may add attribution, but never alter any recorded measurements
        # or the programme, timestamps, identity, and validation of the finish.
        after = cycles[0]
        for key, value in before.items():
            if key in ("emptied_by", "emptied_ts"):
                continue
            if key == "attribution":
                self.assertEqual(after[key].get("start"), value.get("start"))
            else:
                self.assertEqual(after[key], value, key)

    def test_waiting_hours_and_energy_advance_do_not_extend_a_finished_cycle(self):
        for name in APPLIANCES:
            with self.subTest(appliance=name), tempfile.TemporaryDirectory() as tmp:
                clock = dryer.Clock(dryer.T0)
                app = self.finished(name, tmp, clock)
                before, record = measurements(app), dryer.feedback_cycles(app)[0]
                clock.now += timedelta(hours=7)
                set_sensor(app, app.energy_sensor, "15.0")
                door(app, True)
                self.assertEqual(app.state, "Emptied")
                self.assertEqual(measurements(app), before)
                self.assert_ledger_measurements(app, record)

    def test_reload_preserves_the_published_completion(self):
        for name in APPLIANCES:
            with self.subTest(appliance=name), tempfile.TemporaryDirectory() as tmp:
                clock = dryer.Clock(dryer.T0)
                app = self.finished(name, tmp, clock)
                before = measurements(app)
                clock.now += timedelta(hours=3)
                restored = self.make_app(name, tmp, clock, attrs=published(app), previous=app)
                self.assertEqual(restored.state, "Unemptied")
                set_sensor(restored, restored.energy_sensor, "15.0")
                door(restored, True)
                self.assertEqual(measurements(restored), before)

    def test_delayed_running_snapshot_cannot_overwrite_the_published_completion(self):
        for name in APPLIANCES:
            with self.subTest(appliance=name), tempfile.TemporaryDirectory() as tmp:
                clock = dryer.Clock(dryer.T0)
                app = self.started(name, tmp, clock)
                if name == "washer":
                    app._finish_tick_body("Running")
                running_snapshot = app.get_state(app.state_entity, attribute="all")
                self.finish(name, app, clock)
                before, record = measurements(app), dryer.feedback_cycles(app)[0]
                current_get_state = app.get_state

                def delayed_get_state(entity, attribute=None, **kwargs):
                    if entity == app.state_entity and attribute == "all":
                        return running_snapshot
                    return current_get_state(entity, attribute=attribute, **kwargs)

                # The full-state cache still shows Running while the finish publish
                # has already reached HA. Merge updates must leave HA's newer
                # completed summary intact, not copy the stale progress energy.
                app.get_state = delayed_get_state
                try:
                    door(app, True)
                finally:
                    app.get_state = current_get_state
                self.assertEqual(app.state, "Emptied")
                self.assertEqual(measurements(app), before)
                self.assert_ledger_measurements(app, record)

    def test_erased_ha_summary_stays_unknown_despite_a_durable_start_clock(self):
        for name in APPLIANCES:
            with self.subTest(appliance=name), tempfile.TemporaryDirectory() as tmp:
                clock = dryer.Clock(dryer.T0)
                app = self.finished(name, tmp, clock)
                self.assertTrue(app._cycle_store.load())
                clock.now += timedelta(hours=3)
                restored = self.make_app(name, tmp, clock, previous=app, erased=True)
                self.assertEqual(restored.state, "Unemptied")
                self.assertEqual(measurements(restored), {})
                set_sensor(restored, restored.energy_sensor, "15.0")
                door(restored, True)
                self.assertEqual(restored.state, "Emptied")
                self.assertEqual(measurements(restored), {})

    def test_partial_restored_summary_preserves_types_zero_and_missing_values(self):
        snapshots = (
            {"run_time_minutes": "100.1", "end_reason": "history_corrected", "idle_min": 0},
            {"energy_used": None},
            {"run_time_minutes": 0.0, "energy_used": 0.0},
        )
        for name in APPLIANCES:
            for snapshot in snapshots:
                with self.subTest(appliance=name, snapshot=snapshot), tempfile.TemporaryDirectory() as tmp:
                    clock = dryer.Clock(dryer.T0)
                    app = self.make_app(name, tmp, clock, attrs=snapshot)
                    clock.now += timedelta(hours=7)
                    door(app, True)
                    self.assertEqual(measurements(app), snapshot)

    def test_late_emptying_attribution_only_adds_the_actor(self):
        for name in APPLIANCES:
            with self.subTest(appliance=name), tempfile.TemporaryDirectory() as tmp:
                clock = dryer.Clock(dryer.T0)
                app = self.finished(name, tmp, clock)
                before, record = measurements(app), dryer.feedback_cycles(app)[0]
                clock.now += timedelta(hours=7)
                door(app, True)
                empty = dryer.feedback_cycles(app)[0]["attribution"]["empty"]
                verdict = {**empty, "person": "mikkel", "method": "sole_occupant"}
                app._attribute = lambda *a, **kw: verdict
                clock.now += timedelta(minutes=5)
                app._actor_followup_empty({"actor_anchor": empty["anchor"],
                                           "actor_event": f"{name}_emptied"})
                self.assertEqual(published(app)["emptied_by"], "mikkel")
                self.assertEqual(measurements(app), before)
                self.assert_ledger_measurements(app, record)

    def test_reclosing_keeps_each_appliances_existing_off_summary_contract(self):
        for name in APPLIANCES:
            with self.subTest(appliance=name), tempfile.TemporaryDirectory() as tmp:
                clock = dryer.Clock(dryer.T0)
                app = self.finished(name, tmp, clock)
                before = measurements(app)
                clock.now += timedelta(hours=7)
                door(app, True)
                clock.now += timedelta(minutes=10)
                door(app, False)
                self.assertEqual(app.state, "Off")
                if name == "dishwasher":
                    self.assertNotIn("run_time_minutes", published(app))
                    self.assertNotIn("energy_used", published(app))
                else:
                    self.assertEqual(measurements(app), before)

    def test_first_completion_at_emptying_still_measures_the_live_cycle(self):
        for name in APPLIANCES:
            with self.subTest(appliance=name), tempfile.TemporaryDirectory() as tmp:
                clock = dryer.Clock(dryer.T0)
                app = self.make_app(name, tmp, clock)
                set_sensor(app, app.energy_sensor, "10.0")
                if name == "washer":
                    app._begin_running_cycle()
                else:
                    (dryer if name == "dryer" else dishwasher).load_and_start(app, clock)
                clock.now += timedelta(minutes=160, seconds=8)
                set_sensor(app, app.energy_sensor, "11.1234")
                app._transition_to_emptied("cycle finished (door opened first)")
                self.assertEqual(app.state, "Emptied")
                self.assertEqual(published(app)["run_time_minutes"], 160.1)
                self.assertEqual(published(app)["energy_used"], 1.123)

    def test_dryer_next_cycle_does_not_inherit_the_previous_completion_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = dryer.Clock(dryer.T0)
            app = self.finished("dryer", tmp, clock)
            self.assertIs(published(app)["cycle_complete"], True)
            door(app, True)
            door(app, False)
            self.assertEqual(app.state, "Off")
            first_record = dryer.feedback_cycles(app)[0]

            clock.now += timedelta(minutes=11)
            dryer.load_and_start(app, clock)
            self.assertEqual(app.state, "Running")
            self.assertNotIn("cycle_complete", published(app))
            app._update_running_attributes()
            self.assertNotIn("cycle_complete", published(app))

            dryer.finish(app, clock)
            self.assertIs(published(app)["cycle_complete"], True)
            cycles = dryer.feedback_cycles(app)
            self.assertEqual(len(cycles), 2)
            self.assertEqual(cycles[0], first_record)
            self.assertEqual(cycles[1]["duration_min"], 100.0)
            self.assertEqual(cycles[1]["energy_kwh"], 1.0)


if __name__ == "__main__":
    unittest.main()
