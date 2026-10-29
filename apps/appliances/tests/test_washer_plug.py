# tests/test_washer_plug.py - washer_plug: the one window rule every finish decision uses, the
# decisions built on it, the Bomuld tail ETA, the plug read's overall deadline, and the read thread's lifecycle.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import washer_plug as wp  # noqa: E402
from washer_plug_fixture import grid  # noqa: E402

P = wp.POLL_S


def nudge_train(t0, seconds, on_s=4, period_s=11, idle_w=4.0, nudge_w=40.0):
    """Anti-crease: idle at ~4 W with a ~4 s nudge every ~11 s, read every P s."""
    return [(t0 + i * P, nudge_w if (i * P) % period_s < on_s else idle_w) for i in range(int(seconds / P))]


def final_cycle(heat_s=0, strong_s=90, spin_w=450.0, lead_s=240, drain_s=30, train_s=300):
    """Quiet lead-in, optional heating, a strong spin, a drain, then the nudge train, ending on an
    idle read (a tick that lands inside a nudge waits for the next one)."""
    reads = []
    t = 0.0
    for w in [4.0] * int(lead_s / P) + [2000.0] * int(heat_s / P) + [spin_w] * int(strong_s / P) + [17.0] * int(drain_s / P):
        reads.append((t, w))
        t += P
    reads += nudge_train(t, train_s)
    while reads[-1][1] > wp.PULSE_OFF_W:
        reads.pop()
    return reads


def brief_pass_ring():
    """final_cycle cut after the first read at which spin_end holds, then one 12 s tumble and quiet: spin_end holds at
    that read and at no other. Returns (reads, index of that read)."""
    reads = final_cycle(train_s=400)
    i0 = next(i for i in range(len(reads)) if wp.spin_end(reads[:i + 1], reads[i][0]) is not None)
    t = reads[i0][0]
    return reads[:i0 + 1] + [(t + k * P, 40.0) for k in range(1, 7)] + [(t + k * P, 4.0) for k in range(7, 60)], i0


def hold(t0, *parts):
    """Reads on the P grid from t0, each (seconds, watts) part held for its seconds."""
    return grid(t0, [w for seconds, w in parts for _ in range(int(seconds / P))])


def tail_reads(*spins, heat_s=120, after_s=60, base_w=80.0, spin_w=400.0):
    """A heating, then tumbling at base_w with each (start_s, length_s) spin at spin_w, and after_s of tumbling after the
    last spin."""
    end = max(start + length for start, length in spins) + after_s
    return [(k * P, 2000.0 if k * P < heat_s else spin_w if any(s <= k * P < s + n for s, n in spins) else base_w)
            for k in range(int(end / P))]


def tail_eta(reads, until=None):
    """bomuld_tail_eta of a fresh TailTracker fed the reads up to `until`."""
    tracker = wp.TailTracker()
    tracker.feed([r for r in reads if until is None or r[0] <= until])
    return wp.bomuld_tail_eta(tracker.spins)


REAL_WASH = Path(__file__).resolve().parent / "fixtures" / "washer_plug_2026_10_04.json"
TICK_S = 30


def real_wash():
    """The fixture wash's plug as the poller would have read it: every P s, each reported value held until the next.
    Returns (reads, door_s, spin_end_s), seconds from the fixture's start; the door opened at door_s."""
    with open(REAL_WASH, encoding="utf-8") as f:
        data = json.load(f)
    t0 = datetime.fromisoformat(data["t_start"])
    events = [((datetime.fromisoformat(ts) - t0).total_seconds(), float(w)) for ts, w in data["events"]["power"]]
    door_s = (datetime.fromisoformat(data["t_stop"]) - t0).total_seconds()
    held, k, reads = float(data["initial"]["power"]), 0, []
    for i in range(1, int(door_s / P) + 1):
        while k < len(events) and events[k][0] <= i * P:
            held, k = events[k][1], k + 1
        reads.append((i * P, held))
    last_spin = max(i for i, (_, w) in enumerate(reads) if w >= wp.SPIN_W)
    return reads, door_s, reads[last_spin + 1][0]


def first_fire(reads, phase, scan):
    """The first tick (every TICK_S, offset `phase`, from 10 min in) at which the finish rule fires on the reads up to
    it, as (tick, spin end), or None. scan: look back over the reads since the previous tick, else decide at the tick
    instant alone."""
    last = None
    for i, (t, _) in enumerate(reads):
        if t < 600 or (t - phase) % TICK_S:
            continue
        seen = reads[:i + 1]
        end = wp.spin_end_since(seen, t - TICK_S if last is None else last, t) if scan else wp.spin_end(seen, t)
        last = t
        if end is not None:
            return t, end
    return None


class WindowRule(unittest.TestCase):
    def test_consecutive_fresh_reads_covering_the_span_pass(self):
        reads = grid(0, [2.0] * 100)
        self.assertIsNotNone(wp.window(reads, reads[-1][0], 180))

    def test_a_failed_read_voids_the_window(self):
        reads = grid(0, [2.0] * 100)
        reads[70] = (reads[70][0], None)
        self.assertIsNone(wp.window(reads, reads[-1][0], 180))

    def test_reads_60_s_apart_never_make_a_window(self):
        """Review round 4: sample count alone let two reads a minute apart pass standby/recovery."""
        reads = [(t, 2.0) for t in range(0, 300, 60)]
        self.assertIsNone(wp.window(reads, reads[-1][0], 180))
        self.assertFalse(wp.standby(reads, reads[-1][0]))
        self.assertFalse(wp.resumed([(t, 40.0) for t in range(0, 300, 60)], 240))

    def test_a_gap_just_over_the_bound_voids_it(self):
        reads = grid(0, [2.0] * 50) + grid(98 + wp.MAX_GAP_FACTOR * P + 0.1, [2.0] * 60)
        self.assertIsNone(wp.window(reads, reads[-1][0], 180))

    def test_stale_newest_read_voids_it(self):
        reads = grid(0, [2.0] * 100)
        self.assertIsNotNone(wp.window(reads, reads[-1][0] + 2.9, 180))
        self.assertIsNone(wp.window(reads, reads[-1][0] + 3.1, 180))

    def test_ring_must_reach_back_to_the_window_start(self):
        reads = grid(0, [2.0] * 80)            # 158 s of reads
        self.assertIsNone(wp.window(reads, reads[-1][0], 180))


class Standby(unittest.TestCase):
    def test_three_minutes_at_or_below_3_w(self):
        reads = grid(0, [2.8] * 95)
        self.assertTrue(wp.standby(reads, reads[-1][0]))

    def test_soak_and_running_idle_levels_are_not_standby(self):
        """Soak and mid-cycle pauses sit at 3.8-4.3 W; the end-of-programme level is 2.8-3.0 W."""
        for w in (3.1, 3.8, 4.0):
            reads = grid(0, [w] * 95)
            self.assertFalse(wp.standby(reads, reads[-1][0]), w)

    def test_one_nudge_in_the_window_is_not_standby(self):
        ws = [2.8] * 95
        ws[60] = 40.0
        reads = grid(0, ws)
        self.assertFalse(wp.standby(reads, reads[-1][0]))


class SpinEnd(unittest.TestCase):
    def test_final_spin_drain_and_train_is_a_finish(self):
        reads = final_cycle()
        self.assertIsNotNone(wp.spin_end(reads, reads[-1][0]))

    def test_heating_with_a_short_spin_is_not(self):
        """Review round 4's executed counterexample: 30 s at 2000 W then 32 s at 300 W, a drain,
        then nudges. Intact reads reject it (heating)."""
        reads = final_cycle(heat_s=30, strong_s=32, spin_w=300.0)
        self.assertIsNone(wp.spin_end(reads, reads[-1][0]))

    def test_one_failed_heater_read_cannot_turn_it_into_a_spin(self):
        """Review round 4 blocker 1: dropping one heater read took measured heating under 30 s
        while strong time stayed 60 s. Any failed read in the evidence stretch voids it."""
        reads = final_cycle(heat_s=30, strong_s=32, spin_w=300.0)
        i = next(k for k, (_, w) in enumerate(reads) if w == 2000.0)
        reads[i] = (reads[i][0], None)
        self.assertIsNone(wp.spin_end(reads, reads[-1][0]))

    def test_a_failed_read_anywhere_from_lead_in_to_now_voids_it(self):
        base = final_cycle()
        spin_first = next(k for k, (_, w) in enumerate(base) if w >= wp.SPIN_W)
        for k in (spin_first - 10, spin_first + 5, len(base) - 70, len(base) - 3):
            reads = list(base)
            reads[k] = (reads[k][0], None)
            self.assertIsNone(wp.spin_end(reads, reads[-1][0]), k)

    def test_ring_without_the_lead_in_is_no_decision(self):
        """After a restart the ring starts mid-spin: the chain's start is unproven, so no finish."""
        reads = final_cycle()
        spin_first = next(k for k, (_, w) in enumerate(reads) if w >= wp.SPIN_W)
        self.assertIsNone(wp.spin_end(reads[spin_first + 5:], reads[-1][0]))

    def test_decision_waits_while_a_pulse_is_in_progress(self):
        reads = final_cycle()
        while reads[-1][1] <= wp.PULSE_OFF_W:
            reads.pop()
        self.assertIsNone(wp.spin_end(reads, reads[-1][0]))


class SpinEndSince(unittest.TestCase):
    def setUp(self):
        self.ring, self.i0 = brief_pass_ring()
        self.t_pass = self.ring[self.i0][0]
        self.end = wp.spin_end(self.ring[:self.i0 + 1], self.t_pass)
        # the tick 10 s before the pass and the one 20 s after it: the 30 s span a tick interval covers
        self.since, self.now = self.t_pass - 10, self.t_pass + 20
        self.seen = [r for r in self.ring if r[0] <= self.now]

    def test_a_pass_that_holds_at_one_read_between_two_ticks_is_found(self):
        holds = [t for i, (t, _) in enumerate(self.ring) if wp.spin_end(self.ring[:i + 1], t) is not None]
        self.assertEqual(holds, [self.t_pass])
        self.assertIsNotNone(self.end)
        self.assertIsNone(wp.spin_end(self.seen, self.now))
        self.assertEqual(wp.spin_end_since(self.seen, self.since, self.now), self.end)

    def test_a_read_above_the_train_peak_after_the_pass_voids_it(self):
        loud = [(t, wp.TRAIN_PEAK_W + 1 if t == self.t_pass + 14 else w) for t, w in self.seen]
        self.assertEqual(wp.spin_end(loud[:self.i0 + 1], self.t_pass), self.end)
        self.assertIsNone(wp.spin_end_since(loud, self.since, self.now))

    def test_a_failed_read_after_the_pass_voids_it(self):
        failed = [(t, None if t == self.t_pass + 14 else w) for t, w in self.seen]
        self.assertIsNone(wp.spin_end_since(failed, self.since, self.now))

    def test_the_newest_read_must_be_fresh(self):
        self.assertEqual(wp.spin_end_since(self.seen, self.since, self.now + 2.9), self.end)
        self.assertIsNone(wp.spin_end_since(self.seen, self.since, self.now + 3.1))

    def test_a_pass_at_or_before_since_belongs_to_an_earlier_tick(self):
        self.assertIsNone(wp.spin_end_since(self.seen, self.t_pass, self.now))
        self.assertEqual(wp.spin_end_since(self.seen, self.t_pass - 0.1, self.now), self.end)

    def test_the_rule_at_the_tick_instant_still_counts(self):
        """A tick falls between two reads, and the rule holds at that instant before it holds at any read."""
        reads = final_cycle(drain_s=30, train_s=400)
        first = next(i for i in range(len(reads)) if wp.spin_end(reads[:i + 1], reads[i][0]) is not None)
        seen, now = reads[:first], reads[first - 1][0] + 1.5
        self.assertTrue(all(wp.spin_end(seen[:i + 1], t) is None for i, (t, _) in enumerate(seen)))
        end = wp.spin_end(seen, now)
        self.assertIsNotNone(end)
        self.assertEqual(wp.spin_end_since(seen, now - TICK_S, now), end)

    def test_no_pass_in_the_span_is_none(self):
        heated = final_cycle(heat_s=30, strong_s=32, spin_w=300.0)
        self.assertIsNone(wp.spin_end_since(heated, heated[0][0] - 1, heated[-1][0]))
        quiet = grid(0, [4.0] * 300)
        self.assertIsNone(wp.spin_end_since(quiet, quiet[0][0] - 1, quiet[-1][0]))

    def test_a_span_of_one_read_is_the_rule_itself(self):
        reads = final_cycle(train_s=400)
        for i in range(1, len(reads)):
            seen = reads[:i + 1]
            self.assertEqual(wp.spin_end_since(seen, reads[i - 1][0], reads[i][0]), wp.spin_end(seen, reads[i][0]), i)


class RealWash(unittest.TestCase):
    """A real wash (fixtures/washer_plug_2026_10_04.json) whose final spin ended minutes before the door opened and
    where the rule holds only on short stretches between nudges: whatever the tick phase, the finish is found after the
    spin ended and before the door opened, and the same ticks deciding at their own instant alone miss it at some phase."""

    def test_every_tick_phase_finishes_after_the_spin_and_before_the_door(self):
        reads, door_s, spin_end_s = real_wash()
        phases = range(0, TICK_S, int(P))
        self.assertTrue([ph for ph in phases if first_fire(reads, ph, scan=False) is None])
        for ph in phases:
            with self.subTest(phase=ph):
                tick, end = first_fire(reads, ph, scan=True)
                self.assertEqual(end, spin_end_s)
                self.assertGreater(tick, spin_end_s)
                self.assertLess(tick, door_s)


class PulseBound(unittest.TestCase):
    """Pulses are measured between the reads that bracket them, so the measure is never shorter than
    the pulse (review round 4: a 9.99 s pulse counted as four reads, i.e. 8 s)."""

    PHASES = (0.0, 0.01, 0.5, 1.0, 1.5, 1.99)

    def _one_pulse(self, length_s, phase_s):
        reads = [(k * P, 40.0 if phase_s <= k * P < phase_s + length_s else 4.0) for k in range(-5, 20)]
        return reads, next(i for i, (t, _) in enumerate(reads) if t >= 0)

    def test_measure_never_understates(self):
        for length in (3.0, 4.0, 5.3, 7.9, 9.99, 11.0, 15.4):
            for phase in self.PHASES:
                seg, first = self._one_pulse(length, phase)
                (measured, _), = wp._pulses(seg, 0)
                self.assertGreaterEqual(measured, length, (length, phase))
                self.assertLessEqual(measured, length + 2 * P, (length, phase))

    def test_pulses_of_the_limit_or_longer_never_pass(self):
        for length in (wp.PULSE_MAX_S, 15.4, 27.0):
            for phase in self.PHASES:
                seg, _ = self._one_pulse(length, phase)
                self.assertTrue(all(m >= wp.PULSE_MAX_S for m, _ in wp._pulses(seg, 0)), (length, phase))

    def test_a_pulse_still_in_progress_has_no_bound(self):
        seg = [(k * P, 4.0) for k in range(5)] + [(10.0, 40.0), (12.0, 40.0)]
        self.assertIsNone(wp._pulses(seg, 0))

    def test_nudges_pass_at_every_phase(self):
        for phase in self.PHASES:
            reads = nudge_train(phase, 160)
            while reads[-1][1] > wp.PULSE_OFF_W:
                reads.pop()
            self.assertTrue(wp._is_train(reads, reads[-1][0] - wp.TRAIN_S, 0.0), phase)

    def test_one_long_tumble_in_the_window_rejects_the_train(self):
        """07-12 interim window: 5 s nudges plus one 15.4 s tumble must not read as a train."""
        for phase in self.PHASES:
            reads = nudge_train(0.0, 160)
            reads = [(t, 40.0 if 60 + phase <= t < 75.4 + phase else w) for t, w in reads]
            while reads[-1][1] > wp.PULSE_OFF_W:
                reads.pop()
            self.assertFalse(wp._is_train(reads, reads[-1][0] - wp.TRAIN_S, 0.0), phase)


class RecoveryAndHardOff(unittest.TestCase):
    def test_sixty_seconds_of_consecutive_reads_at_or_above_18_w_is_a_resume(self):
        reads = grid(0, [30.0] * 31)
        self.assertTrue(wp.resumed(reads, reads[-1][0]))

    def test_a_nudge_is_not_a_resume(self):
        reads = grid(0, [4.0] * 28 + [40.0] * 3)
        self.assertFalse(wp.resumed(reads, reads[-1][0]))

    def test_hard_off_needs_five_minutes_at_zero(self):
        self.assertTrue(wp.hard_off(grid(0, [0.0] * 151), 300))
        self.assertFalse(wp.hard_off(grid(0, [0.0] * 140), 278))


class TailTracking(unittest.TestCase):
    """TailTracker: the strong spins since the cycle's last heating, kept from call to call."""

    def test_spins_are_the_strong_stretches_after_a_heating(self):
        reads = hold(0, (120, 2000.0), (60, 80.0), (30, 400.0), (240, 80.0), (30, 600.0))
        tracker = wp.TailTracker()
        tracker.feed(reads)
        self.assertEqual(tracker.spins, [[180.0, 208.0], [450.0, 478.0]])
        self.assertEqual((tracker.last_heat, tracker.fed), (118.0, 478.0))

    def test_a_read_at_a_threshold_belongs_to_the_stronger_class(self):
        tracker = wp.TailTracker()
        tracker.feed([(0, wp.HEAT_W), (2, wp.SPIN_STRONG_W - 0.1), (4, wp.SPIN_STRONG_W), (6, wp.HEAT_W - 0.1)])
        self.assertEqual(tracker.spins, [[4, 6]])
        tracker.feed([(8, wp.HEAT_W)], since=6)
        self.assertEqual(tracker.spins, [])
        self.assertEqual(tracker.last_heat, 8)

    def test_strong_reads_up_to_the_spin_gap_apart_are_one_spin(self):
        again = 12 + wp.SPIN_GAP_S
        tracker = wp.TailTracker()
        tracker.feed([(0, 2000.0), (10, 400.0), (12, 400.0), (again, 400.0)])
        self.assertEqual(tracker.spins, [[10, again]])
        tracker = wp.TailTracker()
        tracker.feed([(0, 2000.0), (10, 400.0), (12, 400.0), (again + P, 400.0)])
        self.assertEqual(tracker.spins, [[10, 12], [again + P, again + P]])

    def test_heating_after_a_spin_discards_it(self):
        reads = hold(0, (120, 2000.0), (880, 80.0), (90, 400.0), (200, 80.0), (30, 2000.0), (400, 80.0), (90, 400.0),
                     (60, 80.0))                    # spins at 1000 and 1720, a heating at 1290
        self.assertEqual(tail_eta(reads, 1030), 1000 + wp.BOMULD_TAIL_S[0])
        self.assertIsNone(tail_eta(reads, 1700))
        self.assertEqual(tail_eta(reads, 1750), 1720 + wp.BOMULD_TAIL_S[0])      # the first spin since the heating

    def test_spins_before_any_heating_are_ignored(self):
        reads = hold(0, (500, 80.0), (90, 400.0), (300, 80.0), (60, 2000.0), (400, 80.0), (90, 400.0), (60, 80.0))
        self.assertIsNone(tail_eta(reads, 888))
        self.assertIsNone(tail_eta(reads, 1300))
        self.assertEqual(tail_eta(reads, 1380), 1350 + wp.BOMULD_TAIL_S[0])

    def test_a_burst_under_20_s_is_not_a_spin(self):
        self.assertIsNone(tail_eta(tail_reads((1000, 20))))                  # strong reads 1000..1018: 18 s
        self.assertEqual(tail_eta(tail_reads((1000, 22))), 1000 + wp.BOMULD_TAIL_S[0])      # 1000..1020: 20 s

    def test_a_blip_between_two_spins_is_not_a_step(self):
        reads = tail_reads((1000, 90), (1700, 10), (2140, 150))
        self.assertEqual(tail_eta(reads), 2140 + wp.BOMULD_TAIL_S[1])

    def test_failed_reads_say_nothing(self):
        reads = tail_reads((1000, 90))
        holed = [(t, None) if t == 60 or 1030 <= t <= 1040 else (t, w) for t, w in reads]
        whole, tracker = wp.TailTracker(), wp.TailTracker()
        whole.feed(reads)
        tracker.feed(holed)
        self.assertEqual(whole.spins, [[1000.0, 1088.0]])
        self.assertEqual((tracker.spins, tracker.last_heat, tracker.fed), (whole.spins, whole.last_heat, whole.fed))

    def test_reads_at_or_before_since_are_skipped(self):
        tracker = wp.TailTracker()
        tracker.feed([(0, 2000.0)])
        tracker.feed([(0, 2000.0), (2, 400.0), (4, 400.0)], since=2)
        self.assertEqual((tracker.spins, tracker.last_heat, tracker.fed), ([[4, 4]], 0, 4))

    def test_feeding_in_steps_from_the_newest_read_equals_one_feed(self):
        reads = tail_reads((1000, 90), (2200, 120), (3280, 400))
        whole, steps = wp.TailTracker(), wp.TailTracker()
        whole.feed(reads)
        for end in range(15, len(reads) + 15, 15):           # a call per 30 s tick, each given everything read so far
            steps.feed(reads[:end], steps.fed)
        self.assertEqual(len(whole.spins), 3)
        self.assertEqual((steps.spins, steps.last_heat, steps.fed), (whole.spins, whole.last_heat, whole.fed))

    def test_spins_are_kept_after_their_reads_leave_the_ring(self):
        reads = tail_reads((600, 90), (1740, 120), (2880, 600), after_s=120)         # 60 min: longer than the ring
        ring_len = int(wp.RING_S / P) + 1
        whole, steps, alone = wp.TailTracker(), wp.TailTracker(), wp.TailTracker()
        whole.feed(reads)
        for end in range(15, len(reads) + 15, 15):
            steps.feed(reads[max(0, end - ring_len):end], steps.fed)
        alone.feed(reads[-ring_len:])
        self.assertGreater(reads[-ring_len][0], 600 + 90)            # the first spin and the heating are out of the ring
        self.assertEqual(len(steps.spins), 3)
        self.assertEqual(steps.spins, whole.spins)
        self.assertEqual(alone.spins, [])                            # the ring alone holds no heating


class BomuldTailEta(unittest.TestCase):
    """bomuld_tail_eta: the end from the latest spin and the place the gaps between spin starts give it."""

    def test_no_spin_no_eta(self):
        self.assertIsNone(wp.bomuld_tail_eta([]))

    def test_each_of_three_spins_gives_the_end_from_its_own_start(self):
        reads = tail_reads((1000, 90), (2200, 120), (3280, 300))     # starts 20 and 18 min apart
        self.assertEqual(tail_eta(reads, 1030), 1000 + 50 * 60)
        self.assertEqual(tail_eta(reads, 2230), 2200 + 31 * 60)
        self.assertEqual(tail_eta(reads, 3310), 3280 + 12 * 60)

    def test_a_missing_middle_spin_is_a_double_gap(self):
        for gap_min in (34, 38, 41):
            start = 1000 + gap_min * 60
            reads = tail_reads((1000, 90), (start, 300))
            self.assertEqual(tail_eta(reads), start + 12 * 60, gap_min)

    def test_spins_closer_than_half_a_step_still_count_one_step(self):
        self.assertEqual(wp.bomuld_tail_eta([[0, 30], [300, 330]]), 300 + 31 * 60)

    def test_more_spins_than_the_schedule_stay_at_its_last_step(self):
        self.assertEqual(wp.bomuld_tail_eta([[s, s + 30] for s in (0, 1140, 2280, 3420)]), 3420 + 12 * 60)

    def test_a_spin_of_6_min_is_the_final_one_wherever_it_falls(self):
        first = tail_reads((1000, 600))
        self.assertEqual(tail_eta(first, 1000 + wp.TAIL_FINAL_SPIN_S - 2), 1000 + 50 * 60)
        self.assertEqual(tail_eta(first, 1000 + wp.TAIL_FINAL_SPIN_S), 1000 + 12 * 60)
        second = tail_reads((1000, 90), (2200, 600))                 # the gap alone would make it the second
        self.assertEqual(tail_eta(second, 2200 + wp.TAIL_FINAL_SPIN_S - 2), 2200 + 31 * 60)
        self.assertEqual(tail_eta(second, 2200 + wp.TAIL_FINAL_SPIN_S), 2200 + 12 * 60)


class ReadDeadline(unittest.TestCase):
    """read_switch never takes much longer than its deadline, whether the peer never answers the
    connect, accepts and stays silent, or drips bytes."""

    def _server(self, behaviour):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def serve():
            srv.settimeout(0.2)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    continue
                with conn:
                    behaviour(conn, stop)
        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(lambda: (stop.set(), srv.close()))
        return port

    def _time_read(self, port, deadline=0.5):
        orig = socket.create_connection
        socket.create_connection = lambda addr, timeout=None: orig(("127.0.0.1", port), timeout=timeout)
        try:
            t = time.monotonic()
            with self.assertRaises(Exception):
                wp.read_switch("plug.test", deadline_s=deadline)
            return time.monotonic() - t
        finally:
            socket.create_connection = orig

    def test_silent_peer(self):
        port = self._server(lambda conn, stop: stop.wait(3))
        self.assertLess(self._time_read(port), 0.8)

    def test_dripping_peer(self):
        def drip(conn, stop):
            conn.recv(1024)
            for _ in range(30):
                if stop.is_set():
                    break
                try:
                    conn.sendall(b"H")
                except OSError:
                    break
                time.sleep(0.1)
        port = self._server(drip)
        self.assertLess(self._time_read(port), 0.8)

    def test_answer_is_parsed(self):
        def answer(conn, stop):
            conn.recv(1024)
            body = b'{"id":0,"apower":123.4,"aenergy":{"total":105461.121}}'
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        port = self._server(answer)
        orig = socket.create_connection
        socket.create_connection = lambda addr, timeout=None: orig(("127.0.0.1", port), timeout=timeout)
        try:
            self.assertEqual(wp.read_switch("plug.test", deadline_s=1.0), 123.4)
        finally:
            socket.create_connection = orig


class PollerLifecycle(unittest.TestCase):
    def test_start_stop_and_restart_leave_exactly_one_live_thread(self):
        calls = []
        poller = wp.PlugPoller(lambda: calls.append(1) or 5.0, poll_s=0.02)
        poller.start()
        poller.start()          # the first thread is retired
        time.sleep(0.2)
        live = [t for t in threading.enumerate() if t.name == "washer-plug"]
        self.assertEqual(len(live), 1)
        poller.stop()
        time.sleep(0.1)
        self.assertEqual([t for t in threading.enumerate() if t.name == "washer-plug"], [])
        self.assertTrue(calls)

    def test_a_read_in_flight_across_clear_is_dropped(self):
        """Review round 4: an in-flight read survived stop/clear/start as one stale sample."""
        gate = threading.Event()
        poller = wp.PlugPoller(lambda: gate.wait(1) and 300.0, poll_s=10)
        th = threading.Thread(target=poller.poll_once)
        th.start()
        time.sleep(0.05)
        poller.clear()           # a new cycle starts while the read is out
        gate.set()
        th.join()
        self.assertEqual(poller.snapshot(), [])
        self.assertEqual(poller.peak(), 0.0)

    def test_a_retired_thread_never_appends(self):
        gate = threading.Event()
        poller = wp.PlugPoller(lambda: gate.wait(1) and 300.0, poll_s=10)
        with poller._lock:
            poller._gen += 1
            gen = poller._gen
        th = threading.Thread(target=poller.poll_once, args=(gen,))
        th.start()
        time.sleep(0.05)
        poller.stop()
        gate.set()
        th.join()
        self.assertEqual(poller.snapshot(), [])

    def test_reads_are_timestamped_when_issued_and_failures_kept(self):
        t = [100.0]
        poller = wp.PlugPoller(lambda: 1 / 0, poll_s=2, clock=lambda: t[0])
        poller.poll_once()
        self.assertEqual(poller.snapshot(), [(100.0, None)])
        self.assertEqual(poller.last_ok(), 100.0)   # creation time: never answered


if __name__ == "__main__":
    unittest.main()
