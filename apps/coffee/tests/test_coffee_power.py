# tests/test_coffee_power.py - the machine's class from its plug and the power-path brew detector, on
# real readings of the pour-over plug (fixtures/plug_power.json).
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_power as cp  # noqa: E402

FIXTURES = json.loads((Path(__file__).resolve().parent / "fixtures" / "plug_power.json").read_text())


def events(rows):
    brew, out = cp.PowerBrew(), []
    for ts, value in rows:
        out += brew.feed(ts, value)
    out += brew.tick(rows[-1][0] + 600)
    return out


class MachineMeterTests(unittest.TestCase):
    def test_standby_awake_and_working(self):
        meter = cp.MachineMeter()
        meter.update(0, "0.9")
        self.assertEqual(meter.state(0), "asleep")
        meter.update(10, "2.3")
        self.assertEqual(meter.state(10), "awake")
        meter.update(20, "1043")
        self.assertEqual(meter.state(20), "working")

    def test_working_holds_through_the_quiet_between_pulses(self):
        meter = cp.MachineMeter()
        meter.update(0, "1043")
        meter.update(10, "3.4")
        self.assertEqual(meter.state(40), "working")
        self.assertEqual(meter.state(69), "working")
        self.assertEqual(meter.state(70), "awake")
        meter.update(75, "16")
        meter.update(76, "2.2")
        self.assertEqual(meter.state(130), "working")
        self.assertEqual(meter.state(137), "awake")

    def test_off_when_the_plug_is_off_or_reports_nothing(self):
        meter = cp.MachineMeter()
        self.assertEqual(meter.state(0), "off")
        meter.update(0, "unavailable")
        self.assertEqual(meter.state(0), "off")
        meter.update(1, "1.0")
        meter.plug_on = False
        self.assertEqual(meter.state(1), "off")

    def test_watts_reads_numbers_only(self):
        self.assertEqual([cp.watts(v) for v in ("1.5", 2, "x", None, "nan")], [1.5, 2.0, None, None, None])


class PowerBrewTests(unittest.TestCase):
    def test_a_real_brew_is_one_complete_brew_with_its_times(self):
        rows = FIXTURES["brew_2026_10_17"]
        found = events(rows)
        kinds = [e[0] for e in found]
        self.assertEqual(kinds, ["grinding", "heating", "ended"])
        grinding, heating, ended = found
        self.assertAlmostEqual(heating[2] - grinding[1], 19.5, delta=1.5)
        self.assertEqual(ended[1], grinding[1])
        self.assertTrue(ended[4])
        self.assertGreaterEqual(ended[3], cp.COMPLETE_PULSES)
        self.assertAlmostEqual(ended[2] - ended[1], 175, delta=3)

    def test_grinding_is_reported_once_the_grinder_has_run_eight_seconds(self):
        rows = FIXTURES["brew_2026_10_17"]
        brew = cp.PowerBrew()
        start = None
        for ts, value in rows:
            for event in brew.feed(ts, value):
                if event[0] == "grinding":
                    start = event[1]
                    self.assertGreaterEqual(ts - start, cp.GRINDER_MIN_S)
                    self.assertLess(ts - start, 14)
        self.assertIsNotNone(start)

    def test_a_tick_reports_the_grinding_without_a_new_reading(self):
        brew = cp.PowerBrew()
        self.assertEqual(brew.feed(100, "30"), [])
        self.assertEqual(brew.tick(107), [])
        self.assertEqual(brew.tick(108), [("grinding", 100)])
        self.assertEqual(brew.tick(109), [])

    def test_three_brews_back_to_back_are_three(self):
        found = [e for e in events(FIXTURES["three_brews_2026_08_01"]) if e[0] == "ended"]
        self.assertEqual(len(found), 3)
        self.assertTrue(all(e[4] for e in found))
        for first, second in zip(found, found[1:]):
            self.assertGreater(second[1], first[2])

    def test_pulses_under_500_w_still_count(self):
        found = [e for e in events(FIXTURES["low_heat_brew_2026_10_03"]) if e[0] == "ended"]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0][4])

    def test_grinds_with_no_heat_after_them_are_no_brew(self):
        found = events(FIXTURES["brew_then_grinds_2026_09_12"])
        self.assertEqual([e[0] for e in found], ["grinding", "heating", "ended", "grinding", "grind_only", "grinding",
                                                  "grind_only"])

    def test_heat_with_no_grind_before_it_is_no_brew(self):
        found = events(FIXTURES["grind_then_heat_2026_10_10"])
        self.assertEqual([e[0] for e in found], ["grinding", "grind_only"])

    def test_a_short_blip_is_no_grind(self):
        self.assertEqual(events([(0, "2.2"), (1, "46"), (2, "2.6"), (300, "2.4")]), [])

    def test_a_brew_stopped_after_the_grind_is_incomplete(self):
        rows = [(0, "2"), (1, "40"), (14, "8"), (21, "1000"), (36, "4"), (200, "2")]
        found = events(rows)
        self.assertEqual(found[-1], ("ended", 1, 36, 1, False))

    def test_the_snapshot_restores_a_brew_in_progress(self):
        rows = FIXTURES["brew_2026_10_17"]
        half = len(rows) // 2
        brew = cp.PowerBrew()
        for ts, value in rows[:half]:
            brew.feed(ts, value)
        again = cp.PowerBrew()
        again.restore(json.loads(json.dumps(brew.snapshot())))
        rest = []
        for ts, value in rows[half:]:
            rest += again.feed(ts, value)
        rest += again.tick(rows[-1][0] + 600)
        self.assertEqual(rest[-1][0], "ended")
        self.assertTrue(rest[-1][4])

    def test_restore_ignores_junk(self):
        brew = cp.PowerBrew()
        brew.restore({"phase": "weird", "pulses": 9})
        self.assertEqual((brew.phase, brew.pulses), ("idle", 0))
        brew.restore(None)
        self.assertEqual(brew.phase, "idle")

    def test_detect_lists_the_brews(self):
        rows = FIXTURES["three_brews_2026_08_01"] + FIXTURES["brew_2026_10_17"]
        self.assertEqual(len(cp.detect(rows)), 4)
        self.assertEqual(cp.detect([]), [])


if __name__ == "__main__":
    unittest.main()
