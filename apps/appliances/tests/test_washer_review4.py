# tests/test_washer_review4.py - the seven blockers of adversarial review round 4, each as the
# executed counterexample the review described, driven through the real WasherMonitor
# (make_full_init_app: real initialize(), restore, ticks and transitions; only the AppDaemon
# surface is faked) with the plug's reads fed into its PlugPoller ring.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import types
import unittest
from datetime import timedelta

import test_washer_restart_survival as trs  # installs the appdaemon stub

import washer_plug as wp
from washer_plug_fixture import FakeClock
from test_washer_plug import final_cycle

NOW = trs.NOW
P = wp.POLL_S


def feed(app, watts, clock):
    """Append reads on the 2 s grid, ending at the clock's current time."""
    t0 = clock.t - (len(watts) - 1) * P
    for i, w in enumerate(watts):
        app._plug._ring.append((t0 + i * P, w))
        if w is not None:
            app._plug._last_ok = t0 + i * P
            app._plug._peak = max(app._plug._peak, w)


def running_app(minutes=90, activity=True, **kw):
    """A live Running cycle `minutes` old, restored through an AppDaemon-only reload (the entity
    survived), with a recording Sonos notifier and a real feedback file in a tmpdir."""
    attrs = {"cycle_start_time": trs._iso(NOW - timedelta(minutes=minutes)), "detected_programme": "eco"}
    if activity:
        attrs["activity_seen"] = True
    fb = os.path.join(tempfile.mkdtemp(prefix="washer_r4_"), "washer_feedback.json")
    app = trs.make_full_init_app(sensor_state="Running", sensor_attrs=attrs,
                                 sensor_last_changed=NOW - timedelta(minutes=minutes), power_watts=3.0,
                                 extra_args={"feedback_file": fb}, **kw)
    app.feedback_path = fb
    app.states["input_boolean.washer_announce"] = "on"
    app.sonos_calls = []
    app.sonos_notifier = types.SimpleNamespace(notify=lambda message=None, **k: app.sonos_calls.append(message))
    app.push_calls = []
    app._push_mobile = lambda message: app.push_calls.append(message)
    clock = FakeClock(10_000.0)
    app._plug.clock = clock
    app._plug._last_ok = clock.t
    return app, clock


def records(app):
    if not os.path.exists(app.feedback_path):
        return []
    with open(app.feedback_path) as f:
        return json.load(f).get("cycles", [])


class Blocker1FailedReadsNeverManufactureSpinEvidence(unittest.TestCase):
    def test_one_failed_heater_read_does_not_finish(self):
        app, clock = running_app()
        reads = final_cycle(heat_s=30, strong_s=32, spin_w=300.0)
        i = next(k for k, (_, w) in enumerate(reads) if w == 2000.0)
        reads[i] = (reads[i][0], None)
        clock.t = 10_000.0
        feed(app, [w for _, w in reads], clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Running")
        self.assertEqual(app.sonos_calls, [])

    def test_restore_does_not_rebuild_spin_evidence_from_the_recorder(self):
        """The recorder shows a finished spin and nudges; after a restart the ring is empty, so
        there is no finish until fresh reads show the whole evidence again."""
        start = NOW - timedelta(minutes=120)
        spin_hist = [{"state": w, "last_changed": trs._iso(NOW - timedelta(seconds=s))}
                     for s, w in ((600, "450.0"), (480, "17.0"), (440, "4.0"), (400, "40.0"), (396, "4.0"),
                                  (380, "40.0"), (376, "4.0"), (360, "40.0"), (356, "4.0"))]
        payload = trs.make_store_payload(state="Running", start_time=start, activity_seen=True)
        app = trs.make_full_init_app(sensor_state=None, helper_state="Off", power_watts=4.0,
                                     store_payload=payload, power_history=spin_hist)
        self.assertEqual(app.state, "Running")
        self.assertEqual(app._plug.snapshot(), [])
        self.assertIsNone(app._spin_end_at)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Running")


class Blocker2NoEvidenceCrossesACycleStart(unittest.TestCase):
    def test_missed_door_close_then_new_cycle_with_stalled_reads_is_not_announced(self):
        app, clock = running_app()
        feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(len(app.sonos_calls), 1)
        app._transition_to_emptied("Door opened - emptying")
        self.assertEqual(app.state, "Emptied")
        # The door-close callback never runs; the next wash starts from Emptied.
        app.now += timedelta(minutes=10)
        for w in ("60", "80", "95"):
            app.states[app.power_sensor] = w
            app._power_changed(app.power_sensor, "state", None, w, {})
        self.assertEqual(app.state, "Running")
        self.assertEqual(app._plug.snapshot(), [])
        self.assertFalse(app._activity_seen)
        # Reads stall for six minutes: the ring stays empty, the clock moves on.
        clock.t += 360
        app.now += timedelta(minutes=6)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Running")
        self.assertEqual(len(app.sonos_calls), 1)

    def test_stale_reads_are_no_decision(self):
        app, clock = running_app()
        feed(app, [2.8] * 95, clock)
        clock.t += 4.0          # newest read 4 s old: not fresh
        app._check_energy_finish({})
        self.assertEqual(app.state, "Running")


class Blocker3PublishFailureRollsBackAndRetries(unittest.TestCase):
    def test_unemptied_publish_timeout_leaves_running_then_the_next_tick_announces_once(self):
        app, clock = running_app()
        feed(app, [2.8] * 95, clock)
        real = app.set_state
        failures = [1]

        def set_state(entity, **kw):
            if entity == app.state_entity and kw.get("state") == "Unemptied" and failures:
                failures.pop()
                raise TimeoutError("HA publish timed out")
            return real(entity, **kw)
        app.set_state = set_state

        with self.assertRaises(TimeoutError):
            app._check_energy_finish({})
        self.assertEqual(app.state, "Running")
        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(records(app), [])
        self.assertIsNotNone(app.energy_check_timer)

        clock.t += 30
        feed(app, [2.8] * 15, clock)
        app.now += timedelta(seconds=30)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(len(app.sonos_calls), 1)
        self.assertEqual(len(records(app)), 1)

    def test_rollback_restores_the_cooling_clock(self):
        app, clock = running_app()
        before = app.last_state_change
        app.set_state = lambda entity, **kw: (_ for _ in ()).throw(TimeoutError("down"))
        app.state = "Unemptied"
        app.last_state_change = NOW
        with self.assertRaises(TimeoutError):
            app._set_state_entity(state="Unemptied", attributes={})
        self.assertEqual(app.state, "Running")
        self.assertEqual(app.last_state_change, before)


class Blocker4EvidenceSurvivesReloadAndUpgrade(unittest.TestCase):
    def test_ad_reload_keeps_wash_activity(self):
        app, clock = running_app(activity=True)
        self.assertTrue(app._activity_seen)
        feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Unemptied")

    def test_head_state_without_activity_derives_it_from_the_recorder(self):
        start = NOW - timedelta(minutes=90)
        hist = [{"state": "450.0", "last_changed": trs._iso(start + timedelta(minutes=70))},
                {"state": "3.9", "last_changed": trs._iso(start + timedelta(minutes=72))}]
        app, clock = running_app(activity=False, power_history=hist)
        self.assertTrue(app._activity_seen)
        feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Unemptied")

    def test_no_activity_anywhere_is_never_a_standby_finish(self):
        app, clock = running_app(activity=False, power_history=[{"state": "3.9", "last_changed": trs._iso(NOW)}])
        self.assertFalse(app._activity_seen)
        feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Running")
        self.assertEqual(app.sonos_calls, [])

    def test_ha_restart_store_carries_activity(self):
        payload = trs.make_store_payload(state="Running", start_time=NOW - timedelta(minutes=90), activity_seen=True)
        app = trs.make_full_init_app(sensor_state=None, helper_state="Off", power_watts=4.0, store_payload=payload)
        self.assertTrue(app._activity_seen)
        self.assertTrue(app._build_cycle_store_payload("Running")["activity_seen"])


class Blocker5HaPowerNeverOverridesTheReads(unittest.TestCase):
    def test_ha_power_unavailable_does_not_end_the_cycle_and_reads_still_decide(self):
        app, clock = running_app()
        app.states[app.power_sensor] = "unavailable"
        app._handle_unavailable(app.power_sensor, None, "3.0", "unavailable", {})
        app._power_changed(app.power_sensor, "state", "3.0", "unavailable", {})
        app.now += timedelta(minutes=4)
        self.assertEqual(app.state, "Running")
        feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Unemptied")


class Blocker6CounterResetIsIrrelevant(unittest.TestCase):
    def test_energy_counter_reset_mid_cycle_does_not_block_the_finish(self):
        app, clock = running_app()
        app.energy_start = 120.0
        app.states[app.energy_sensor] = "0.5"
        feed(app, [2.8] * 95, clock)
        app._check_energy_finish({})
        self.assertEqual(app.state, "Unemptied")


class Blocker7ReaderLifecycle(unittest.TestCase):
    def test_initialize_schedules_the_reader_and_terminate_retires_it(self):
        app, clock = running_app()
        self.assertIn((app._start_plug_poller, 0), [(cb, d) for cb, d, _ in app.scheduled])
        poller = wp.PlugPoller(lambda: 1.0, poll_s=0.02)
        app._plug = poller
        app._start_plug_poller({})
        time.sleep(0.1)
        self.assertEqual(len([t for t in threading.enumerate() if t.name == "washer-plug"]), 1)
        app.terminate()
        time.sleep(0.1)
        self.assertEqual([t for t in threading.enumerate() if t.name == "washer-plug"], [])


class OutagePolicy(unittest.TestCase):
    def test_ten_silent_minutes_while_running_push_once(self):
        app, clock = running_app()
        clock.t += 601
        app._check_energy_finish({})
        app._check_energy_finish({})
        self.assertEqual(len(app.push_calls), 1)
        self.assertIn("finish detection is paused", app.push_calls[0])

    def test_no_second_page_when_ha_already_paged_the_same_outage(self):
        app, clock = running_app()
        app._plug_outage_pushed = True
        clock.t += 601
        app._check_energy_finish({})
        self.assertEqual(app.push_calls, [])


if __name__ == "__main__":
    unittest.main()
