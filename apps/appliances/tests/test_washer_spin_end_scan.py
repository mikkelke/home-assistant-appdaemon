# tests/test_washer_spin_end_scan.py - the finish tick looks for the spin-end finish on every plug read since its
# previous tick, driven through the real WasherMonitor (test_washer_review4.running_app: real initialize(), ticks and
# transitions; only the AppDaemon surface is faked) with the reads fed into its PlugPoller ring.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import unittest
from datetime import timedelta

import test_washer_restart_survival as trs  # installs the appdaemon stub
import test_washer_review4 as r4

import washer_plug as wp
from test_washer_plug import TICK_S, brief_pass_ring, first_fire, real_wash

NOW = trs.NOW
BASE = 10_000.0     # the plug clock's reading at second 0 of a series, as running_app sets it


def tick(app, clock, reads, t):
    """Feed the series' reads up to second t that the ring does not hold yet, move both clocks to t and run one finish
    tick."""
    ring = app._plug._ring
    for ts, w in reads[len(ring):]:
        if ts > t:
            break
        ring.append((BASE + ts, w))
        if w is not None:
            app._plug._last_ok = BASE + ts
            app._plug._peak = max(app._plug._peak, w)
    clock.t = BASE + t
    app.now = NOW + timedelta(seconds=t)
    app._check_energy_finish({})


class FinishTickScansSinceItsPreviousTick(unittest.TestCase):
    def test_a_pass_between_two_ticks_finishes_on_the_later_tick(self):
        app, clock = r4.running_app()
        reads, i0 = brief_pass_ring()
        t_pass = reads[i0][0]
        end = wp.spin_end(reads[:i0 + 1], t_pass)

        tick(app, clock, reads, t_pass - 10)
        self.assertEqual(app.state, "Running")
        self.assertEqual(app._plug.checked_at, clock.t)

        tick(app, clock, reads, reads[-1][0])       # 130 s on, well past the one-interval look-back
        self.assertIsNone(wp.spin_end(app._plug.snapshot(), clock.t))
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(len(app.sonos_calls), 1)
        self.assertAlmostEqual((app._spin_end_at - NOW).total_seconds(), end, places=3)

    def test_the_real_wash_is_announced_before_the_door_at_a_phase_the_instant_rule_missed(self):
        reads, door_s, spin_end_s = real_wash()
        phase = next(ph for ph in range(0, TICK_S, int(wp.POLL_S)) if first_fire(reads, ph, scan=False) is None)
        app, clock = r4.running_app(minutes=150)
        t = 600 + (phase - 600) % TICK_S
        while app.state == "Running" and t < door_s:
            tick(app, clock, reads, t)
            t += TICK_S
        self.assertEqual(app.state, "Unemptied")
        self.assertLess(t - TICK_S, door_s)
        self.assertEqual(len(app.sonos_calls), 1)
        self.assertAlmostEqual((app._spin_end_at - NOW).total_seconds(), spin_end_s, places=3)


class CycleBoundariesRestartTheScan(unittest.TestCase):
    def test_boot_a_new_cycle_and_a_recovered_one_start_with_no_previous_tick(self):
        app = trs.make_full_init_app(sensor_state=None, helper_state=None, power_watts=0, now=NOW)
        self.assertIsNone(app._plug.checked_at)

        app._plug.checked_at = 123.0
        app._begin_running_cycle()
        self.assertIsNone(app._plug.checked_at)

        app._plug.checked_at = 123.0
        app.state = "Unemptied"
        app.states[app.state_entity] = "Unemptied"
        app.attrs_store.setdefault(app.state_entity, {})["run_time_minutes"] = 120
        app._recover_from_false_unemptied(60.0)
        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._plug.checked_at)



class ProcessRestartRebuildsTheRingFromTheRecorder(unittest.TestCase):
    """A new AppDaemon process starts with an empty ring a minute before the final spin of the fixture wash ends;
    the recorder holds the plug's reported values up to the restart."""

    def restarted_wash(self, ha_started_s=None):
        reads, door_s, spin_end_s = real_wash()
        restart = spin_end_s - 60
        app, clock = r4.running_app(minutes=150)
        ha_start = NOW + timedelta(seconds=ha_started_s) if ha_started_s is not None else NOW - timedelta(days=1)
        app.states[app.ha_uptime_sensor] = trs._iso(ha_start)
        app.get_history = lambda **kw: [[{"state": str(w), "last_changed": trs._iso(NOW + timedelta(seconds=ts))}
                                         for ts, w in reads if ts < restart]]
        live = iter([(ts, w) for ts, w in reads if ts >= restart])
        pending = [next(live)]

        def run_to(t):
            while pending and pending[0][0] <= t:
                ts, w = pending.pop()
                app._plug._ring.append((BASE + ts, w))
                app._plug._last_ok = BASE + ts
                nxt = next(live, None)
                if nxt is not None:
                    pending.append(nxt)
            clock.t = BASE + t
            app.now = NOW + timedelta(seconds=t)

        run_to(restart + 16)
        return app, run_to, restart, door_s, spin_end_s

    def finish_before(self, app, run_to, t, door_s):
        while app.state == "Running" and t < door_s:
            run_to(t)
            app._check_energy_finish({})
            t += TICK_S
        return app.state

    def test_the_rebuilt_reads_announce_the_final_spin(self):
        app, run_to, restart, door_s, spin_end_s = self.restarted_wash()
        app._rebuild_plug_reads({})
        ring = app._plug.snapshot()
        self.assertEqual(ring[0][0], BASE + real_wash()[0][0][0])
        self.assertEqual({round(b[0] - a[0], 6) for a, b in zip(ring, ring[1:])}, {wp.POLL_S})
        self.assertEqual(self.finish_before(app, run_to, restart + 30, door_s), "Unemptied")
        self.assertAlmostEqual((app._spin_end_at - NOW).total_seconds(), spin_end_s, places=3)

    def test_without_the_rebuild_the_door_comes_first(self):
        app, run_to, restart, door_s, spin_end_s = self.restarted_wash()
        self.assertEqual(self.finish_before(app, run_to, restart + 30, door_s), "Running")

    def test_nothing_from_before_ha_started_is_rebuilt(self):
        ha_started_s = real_wash()[2] - 600
        app, run_to, restart, door_s, spin_end_s = self.restarted_wash(ha_started_s=ha_started_s)
        app._rebuild_plug_reads({})
        self.assertGreaterEqual(app._plug.snapshot()[0][0], BASE + ha_started_s + 60)
        self.assertLess(app._plug.snapshot()[0][0], BASE + ha_started_s + 60 + wp.POLL_S)


if __name__ == "__main__":
    unittest.main()
