# tests/test_washer_review5.py - the three fixes from adversarial review round 5, each as the
# executed counterexample the review described.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import types
import unittest
from datetime import timedelta

import test_washer_restart_survival as trs  # installs the appdaemon stub
import test_washer_review4 as r4
from washer_plug_fixture import FakeClock

NOW = trs.NOW


class RestoreTrustsACompletedStoreOverAStaleRunningEntity(unittest.TestCase):
    """P2 (must fix): _set_state_entity can "succeed" locally (store written, self.state
    advanced) while the HTTP publish to HA never lands, leaving the entity showing the
    PREVIOUS state. A process restart must not re-run finish detection (and re-announce) for
    a cycle the durable store already completed and notified."""

    def test_stale_running_entity_with_a_completed_matching_store_restores_quietly(self):
        start = NOW - timedelta(minutes=100)
        store_payload = trs.make_store_payload(
            state="Unemptied", start_time=start, cycle_id="cycle-dup",
            notification_sent=True,
        )
        sensor_attrs = {"cycle_start_time": trs._iso(start), "detected_programme": "eco"}
        app = trs.make_full_init_app(
            sensor_state="Running", sensor_attrs=sensor_attrs, sensor_last_changed=start,
            power_watts=2.0, store_payload=store_payload,
        )

        # Trusts the completed store, not the stale live "Running".
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app._cycle_id, "cycle-dup")
        self.assertTrue(app.notification_sent)

        # Republishes the corrected state to HA.
        published = trs.last_publish(app)
        self.assertIsNotNone(published)
        self.assertEqual(published["state"], "Unemptied")

        # Exactly one announcement overall: the door-unlock announce path (the other route to
        # Sonos for an already-Unemptied cycle) must see notification_sent already True and
        # stay silent - proving notification identity was not reset by the restore.
        sonos_calls = []
        app.sonos_notifier = types.SimpleNamespace(notify=lambda **kw: sonos_calls.append(kw))
        app.door_lock_entity = "lock.washer_door"
        app._door_lock_state_changed(app.door_lock_entity, "state", "locked", "unlocked", {})
        self.assertEqual(sonos_calls, [])

    def test_a_genuinely_newer_running_cycle_still_restores_as_running(self):
        """Negative control: the store holds an OLDER completed+notified cycle with a DIFFERENT
        start_time than the live entity's own Running clock - a real new cycle, not a lost
        publish. Must restore as Running with the live entity's own clock, unmodified."""
        older_start = NOW - timedelta(hours=6)
        live_start = NOW - timedelta(minutes=30)
        store_payload = trs.make_store_payload(
            state="Unemptied", start_time=older_start, cycle_id="cycle-old",
            notification_sent=True,
        )
        sensor_attrs = {"cycle_start_time": trs._iso(live_start), "detected_programme": "eco"}
        app = trs.make_full_init_app(
            sensor_state="Running", sensor_attrs=sensor_attrs, sensor_last_changed=live_start,
            power_watts=40, store_payload=store_payload,
        )
        self.assertEqual(app.state, "Running")
        self.assertLessEqual(abs((app.start_time - live_start).total_seconds()), 2)
        # No spurious republish - the live entity already agreed with itself.
        self.assertEqual(app.set_state_calls, [])


class ActivityRecorderFailureRetriesInsteadOfBecomingFalse(unittest.TestCase):
    """P2 (must fix): the one-time boot lookup for activity_seen must not turn a transient
    recorder failure into a hard False - it stays unknown (None) and is retried on later
    finish-decision ticks until it succeeds or the cycle ends."""

    def test_recorder_unavailable_at_init_then_healthy_finishes(self):
        start = NOW - timedelta(minutes=90)
        calls = {"n": 0}

        def flaky_history(entity_id=None, start_time=None, end_time=None, **kw):
            # Restore also runs an unconditional, unrelated power-history cross-check (a
            # different window: start_time - 5min) - only the activity_seen lookup itself
            # (queried at exactly self.start_time) is made to fail here.
            if entity_id != "sensor.washer_plug_power" or start_time != start:
                return [[]]
            calls["n"] += 1
            if calls["n"] <= 2:
                raise TimeoutError("recorder unavailable")
            return [[{"state": "450", "last_changed": trs._iso(start + timedelta(minutes=5))}]]

        sensor_attrs = {"cycle_start_time": trs._iso(start), "detected_programme": "eco"}
        app = trs.make_full_init_app(
            sensor_state="Running", sensor_attrs=sensor_attrs, sensor_last_changed=start,
            power_watts=3.0, get_history_fn=flaky_history,
        )
        clock = FakeClock(10_000.0)
        app._plug.clock = clock
        app._plug._last_ok = clock.t

        # Boot-time lookup (call 1) failed: unknown, never coerced to False.
        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._activity_seen)
        self.assertEqual(calls["n"], 1)

        # A finish-decision tick with the recorder still down (call 2, the first retry) must
        # neither finish nor fall through to a false "Off" - it stays Running, still unknown.
        r4.feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._activity_seen)
        self.assertEqual(calls["n"], 2)

        # Once the retry throttle window has passed and the recorder is healthy again (call 3),
        # the very next tick resolves activity_seen and finishes.
        app.now += timedelta(seconds=310)
        clock.t += 310
        r4.feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(calls["n"], 3)
        self.assertTrue(app._activity_seen)
        self.assertEqual(app.state, "Unemptied")


class DirectRunningToEmptiedPublishesTheCorrectedDuration(unittest.TestCase):
    """P3: publishing (line ~3606) used to precede the duration correction (was ~3548 further
    down), so a direct Running -> Emptied (e.g. door opened at the exact moment of finish)
    published the uncorrected wall-clock duration while the feedback record got the corrected
    one. Both must now agree."""

    def test_published_duration_matches_the_feedback_duration(self):
        app, clock = r4.running_app(minutes=100)
        app._correct_duration = lambda wall, log_prefix=None: (80.0, "power_history")

        app._transition_to_emptied("door_opened_first")

        self.assertEqual(app.state, "Emptied")
        emptied_calls = [c for c in app.set_state_calls if c["state"] == "Emptied"]
        self.assertEqual(len(emptied_calls), 1)
        published_minutes = emptied_calls[-1]["attributes"]["run_time_minutes"]
        record = r4.records(app)[-1]
        self.assertEqual(published_minutes, 80.0)
        self.assertEqual(record["duration_min"], 80.0)
        self.assertEqual(published_minutes, record["duration_min"])


if __name__ == "__main__":
    unittest.main()
