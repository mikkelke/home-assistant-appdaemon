"""Emptying must not turn idle waiting into additional wash time or energy."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta

from test_dishwasher_attribution import (
    Clock, T0, ENERGY_SENSOR, make_app, load_and_start,
    finish, open_door, close_door, published, feedback_cycles,
)


class CompletedStatistics(unittest.TestCase):
    def finished(self, tmp, clock):
        app = make_app(tmp, clock)
        app.initialize()
        load_and_start(app, clock)
        finish(app, clock)
        self.assertEqual(published(app)["run_time_minutes"], 235.0)
        return app

    def test_hours_waiting_and_standby_energy_do_not_change_completed_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            app = self.finished(tmp, clock)
            before = dict(published(app))
            record = dict(feedback_cycles(app)[-1])
            clock.now += timedelta(hours=7)
            app.entities[ENERGY_SENSOR]["state"] = "9.0"
            open_door(app)
            self.assertEqual(app.state, "Emptied")
            for key in ("run_time_minutes", "energy_used"):
                self.assertEqual(published(app)[key], before[key])
            after = dict(feedback_cycles(app)[-1])
            for key, value in record.items():
                if key == "attribution":
                    self.assertEqual(after[key]["start"], value["start"])
                    continue
                self.assertEqual(after[key], value)

    def test_peek_and_later_emptying_keep_the_same_finished_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            app = self.finished(tmp, clock)
            before = dict(published(app))
            clock.now += timedelta(hours=3)
            open_door(app)
            clock.now += timedelta(seconds=10)
            close_door(app)
            self.assertEqual(app.state, "Unemptied")
            clock.now += timedelta(hours=2)
            open_door(app)
            for key in ("run_time_minutes", "energy_used"):
                self.assertEqual(published(app)[key], before[key])

    def test_reload_before_emptying_preserves_published_finished_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            app = self.finished(tmp, clock)
            before = dict(published(app))
            clock.now += timedelta(hours=3)
            restored = make_app(tmp, clock, sensor_state="Unemptied", sensor_attrs=before,
                                helper_state="Unemptied", energy_kwh="9.0")
            restored.initialize()
            open_door(restored)
            for key in ("run_time_minutes", "energy_used"):
                self.assertEqual(published(restored)[key], before[key])

    def test_erased_summary_after_ha_restart_does_not_invent_completion_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock(T0)
            self.finished(tmp, clock)
            clock.now += timedelta(hours=3)
            restored = make_app(tmp, clock, helper_state="Unemptied", energy_kwh="9.0")
            restored.initialize()
            open_door(restored)
            self.assertEqual(restored.state, "Emptied")
            self.assertNotIn("run_time_minutes", published(restored))
            self.assertNotIn("energy_used", published(restored))
            clock.now += timedelta(seconds=10)
            close_door(restored)
            self.assertEqual(restored.state, "Unemptied")
            self.assertNotIn("run_time_minutes", published(restored))


if __name__ == "__main__":
    unittest.main()
