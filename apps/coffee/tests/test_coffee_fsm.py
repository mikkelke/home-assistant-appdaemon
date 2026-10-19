# tests/test_coffee_fsm.py - a brew's lifecycle from the integration's, the scale's and the plug's inputs:
# our start, a machine start, the plug-only path, stops and faults, pickup and the lifetimes.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_fsm as cf  # noqa: E402

T = 1_800_000_000.0


def started(tracker, at=T, brew_id="c1"):
    tracker.request(brew_id, at, "tablet")
    return tracker.accepted(brew_id, "run1", "4 Pour Recipe", at + 6)


def brewing(tracker, at=T):
    started(tracker, at)
    return tracker.pour(at + 28, 0)


class StartTests(unittest.TestCase):
    def test_our_start_goes_starting_then_grinding_on_the_machine_accepting(self):
        tracker = cf.Tracker()
        self.assertEqual(tracker.request("c1", T, "tablet"), ["state"])
        self.assertEqual(tracker.phase(), "starting")
        self.assertEqual(tracker.accepted("c1", "run1", "4 Pour Recipe", T + 6), ["started", "state", "live"])
        self.assertEqual((tracker.phase(), tracker.brew["run_id"], tracker.brew["started_ts"]),
                         ("grinding", "run1", T + 6))

    def test_a_second_start_while_starting_does_nothing(self):
        tracker = cf.Tracker()
        tracker.request("c1", T, "tablet")
        self.assertEqual(tracker.request("c2", T + 2, "phone"), [])
        self.assertEqual(tracker.brew["id"], "c1")

    def test_the_grinder_seen_before_the_acceptance_is_our_brew(self):
        tracker = cf.Tracker()
        tracker.request("c1", T, "tablet")
        self.assertIn("started", tracker.grinding_seen(T + 5))
        self.assertEqual(tracker.accepted("c1", "run1", None, T + 6), [])
        self.assertEqual((tracker.brew["id"], tracker.brew["source"], tracker.brew["run_id"]), ("c1", "tablet", "run1"))

    def test_a_refusal_is_start_failed_with_the_machines_reason(self):
        tracker = cf.Tracker()
        tracker.request("c1", T, "tablet")
        self.assertEqual(tracker.refused("c1", "run1", "not_on_home_screen", T + 4), ["start_failed", "state"])
        self.assertEqual((tracker.phase(), tracker.brew["start_error"]), ("start_failed", "not_on_home_screen"))

    def test_someone_elses_refusal_is_ignored(self):
        tracker = cf.Tracker()
        tracker.request("c1", T, "tablet")
        self.assertEqual(tracker.refused("c9", None, "machine_busy", T + 4), [])
        self.assertEqual(tracker.refused(None, None, "machine_busy", T + 4), [])
        self.assertEqual(tracker.phase(), "starting")

    def test_no_answer_within_45_s(self):
        tracker = cf.Tracker()
        tracker.request("c1", T, "tablet")
        self.assertEqual(tracker.tick(T + 44), [])
        self.assertEqual(tracker.tick(T + 45), ["start_failed", "state"])
        self.assertEqual(tracker.brew["start_error"], "no_answer")

    def test_start_failed_lasts_a_minute(self):
        tracker = cf.Tracker()
        tracker.request("c1", T, "tablet")
        tracker.refused("c1", None, "machine_busy", T + 3)
        self.assertEqual(tracker.tick(T + 62), [])
        self.assertEqual(tracker.tick(T + 63), ["expired", "state"])
        self.assertIsNone(tracker.phase())
        self.assertEqual(tracker.last["id"], "c1")

    def test_a_stop_before_the_machine_took_it(self):
        tracker = cf.Tracker()
        tracker.request("c1", T, "tablet")
        self.assertEqual(tracker.stop_asked(T + 2), ["closed", "state", "live"])
        self.assertEqual((tracker.phase(), tracker.brew["stop_reason"], tracker.brew["started_ts"]),
                         ("stopped", "home_assistant", None))


class OtherStartTests(unittest.TestCase):
    def test_a_machine_start(self):
        tracker = cf.Tracker()
        self.assertEqual(tracker.grinding_seen(T, "machine"), ["started", "state", "live"])
        self.assertEqual((tracker.brew["id"], tracker.brew["source"]), ("c1800000000", "machine"))
        self.assertEqual(tracker.grinding_seen(T + 1, "machine"), [])

    def test_a_home_assistant_start_not_from_us(self):
        tracker = cf.Tracker()
        self.assertEqual(tracker.accepted(None, "run7", "4 Pour Recipe", T), ["started", "state", "live"])
        self.assertEqual((tracker.brew["source"], tracker.brew["recipe_name"]), ("ha", "4 Pour Recipe"))

    def test_an_adopted_machine_start_keeps_its_source(self):
        tracker = cf.Tracker()
        tracker.grinding_seen(T, "machine")
        self.assertEqual(tracker.accepted(None, "run8", "4 Pour Recipe", T + 1), [])
        self.assertEqual((tracker.brew["source"], tracker.brew["run_id"]), ("machine", "run8"))

    def test_a_new_brew_while_one_is_ready_replaces_it(self):
        tracker = cf.Tracker()
        brewing(tracker)
        tracker.enjoy(T + 240)
        tracker.grinding_seen(T + 600, "machine")
        self.assertEqual((tracker.phase(), tracker.last["id"]), ("grinding", "c1"))


class BrewTests(unittest.TestCase):
    def test_bloom_pours_and_the_curve(self):
        tracker = cf.Tracker()
        effects = brewing(tracker)
        self.assertEqual(effects, ["bloom", "state", "live"])
        self.assertEqual((tracker.phase(), tracker.brew["pour"], tracker.brew["bloom_ts"]), ("brewing", 1, T + 28))
        self.assertEqual(tracker.pour(T + 74, 1), ["state"])
        self.assertEqual(tracker.pour(T + 75, 1), [])
        self.assertEqual(tracker.curve.pours, [[0, 0], [46, 1]])
        tracker.frame(T + 30)
        tracker.reading_cup(T + 30, 3.0)
        tracker.reading_water(T + 30, 20.0)
        self.assertEqual(tracker.tick(T + 30), ["live"])
        self.assertEqual(tracker.curve.cup, [30, 30, 30])

    def test_the_brew_status_is_a_second_way_to_the_bloom(self):
        tracker = cf.Tracker()
        started(tracker)
        self.assertIn("bloom", tracker.bloom(T + 28))
        self.assertEqual(tracker.bloom(T + 29), [])

    def test_pause_and_resume_return_to_where_it_was(self):
        tracker = cf.Tracker()
        brewing(tracker)
        self.assertEqual(tracker.paused(True, T + 60), ["state", "live"])
        self.assertEqual(tracker.phase(), "paused")
        self.assertEqual(tracker.paused(False, T + 90), ["state", "live"])
        self.assertEqual(tracker.phase(), "brewing")
        self.assertEqual(tracker.curve.pauses, [[32, 62]])
        tracker2 = cf.Tracker()
        started(tracker2)
        tracker2.paused(True, T + 10)
        tracker2.paused(False, T + 20)
        self.assertEqual(tracker2.phase(), "grinding")

    def test_enjoy_makes_it_ready_once(self):
        tracker = cf.Tracker()
        brewing(tracker)
        tracker.brew_end(T + 170)
        self.assertEqual(tracker.enjoy(T + 235), ["ready", "state", "live"])
        self.assertEqual(tracker.enjoy(T + 236), [])
        self.assertEqual(tracker.completed({"outcome": "confirmed", "run_id": "run1", "duration_s": 207}, T + 236), [])
        self.assertEqual(tracker.brew["completed"]["duration_s"], 207)
        self.assertEqual((tracker.brew["enjoy_ts"], tracker.brew["brew_end_ts"], tracker.curve.end_s),
                         (T + 235, T + 170, 207))

    def test_a_presumed_completion_makes_it_ready_without_enjoy(self):
        tracker = cf.Tracker()
        brewing(tracker)
        data = {"outcome": "presumed", "context": {"coffee_brew_id": "c1"}, "first_pour_at": None,
                "ended_at": "2030-01-15T08:03:40+00:00"}
        self.assertEqual(tracker.completed(data, T + 300), ["ready", "state", "live"])
        self.assertNotIn("enjoy_ts", tracker.brew)
        self.assertEqual(tracker.completed({"outcome": "presumed"}, T + 301), [])

    def test_stops(self):
        cases = [
            (lambda t: t.stopped("c1", "run1", "home_assistant", True, T + 100), "home_assistant", "stopped"),
            (lambda t: t.stopped(None, "run1", "machine", True, T + 100), "machine", "stopped"),
            (lambda t: t.refused(None, "run1", "no_beans", T + 100), "fault", "failed"),
            (lambda t: t.status_idle(T + 100), "machine", "stopped"),
            (lambda t: t.fault("no_beans", T + 100), "fault", "failed"),
            (lambda t: t.abandon(T + 100), "abandoned", "stopped"),
        ]
        for act, reason, outcome in cases:
            tracker = cf.Tracker()
            brewing(tracker)
            self.assertEqual(act(tracker), ["closed", "state", "live"], reason)
            self.assertEqual((tracker.phase(), tracker.brew["stop_reason"], tracker.brew["outcome"]),
                             ("stopped", reason, outcome))
            self.assertEqual(tracker.curve.end_s, 72)

    def test_a_stop_for_another_run_is_ignored(self):
        tracker = cf.Tracker()
        brewing(tracker)
        self.assertEqual(tracker.stopped(None, "run9", "machine", True, T + 100), [])

    def test_the_machine_stopping_after_our_stop_is_ours(self):
        tracker = cf.Tracker()
        tracker.grinding_seen(T, "machine")
        tracker.stop_asked(T + 10)
        tracker.status_idle(T + 14)
        self.assertEqual(tracker.brew["stop_reason"], "home_assistant")

    def test_a_stop_while_grinding_keeps_whether_it_ground(self):
        tracker = cf.Tracker()
        started(tracker)
        tracker.stopped("c1", "run1", "machine", False, T + 10)
        self.assertFalse(tracker.brew["ground"])
        tracker = cf.Tracker()
        started(tracker)
        tracker.stopped("c1", "run1", "machine", True, T + 25)
        self.assertTrue(tracker.brew["ground"])

    def test_no_water_mid_brew_is_a_fault_once(self):
        tracker = cf.Tracker()
        brewing(tracker)
        self.assertEqual(tracker.fault("no_water", T + 80), ["fault", "state"])
        self.assertEqual(tracker.fault("no_water", T + 81), [])
        self.assertEqual(tracker.fault("ok", T + 82), [])
        self.assertEqual(tracker.brew["faults"], ["no_water"])
        self.assertEqual(tracker.phase(), "brewing")

    def test_faults_when_no_brew_runs_are_not_a_brews(self):
        self.assertEqual(cf.Tracker().fault("no_water", T), [])


class ReadyTests(unittest.TestCase):
    def ready(self):
        tracker = cf.Tracker()
        brewing(tracker)
        tracker.enjoy(T + 235)
        tracker.brew["final"] = {"cup_g": 217.0}
        return tracker

    def test_the_cup_lifted_for_three_seconds_on_a_fresh_link_is_picked_up(self):
        tracker = self.ready()
        tracker.reading_cup(T + 300, 216.9)
        self.assertEqual(tracker.reading_cup(T + 301, 0.4), [])
        self.assertEqual(tracker.tick(T + 303), [])
        self.assertEqual(tracker.tick(T + 304), ["picked_up", "state"])
        self.assertEqual(tracker.brew["picked_up_ts"], T + 304)

    def test_a_dip_or_a_stale_link_is_no_pickup(self):
        tracker = self.ready()
        tracker.reading_cup(T + 300, 30.0)
        tracker.reading_cup(T + 301, 100.0)
        self.assertEqual(tracker.tick(T + 305), [])
        tracker.reading_cup(T + 306, 0.0)
        self.assertEqual(tracker.tick(T + 320), [])
        self.assertEqual(tracker.phase(), "ready")

    def test_a_small_cup_lifted_uses_a_quarter_of_its_weight(self):
        tracker = self.ready()
        tracker.brew["final"] = {"cup_g": 120.0}
        tracker.reading_cup(T + 300, 35.0)
        tracker.tick(T + 304)
        self.assertEqual(tracker.phase(), "ready")
        tracker.reading_cup(T + 305, 29.0)
        tracker.reading_cup(T + 306, 29.0)
        tracker.frame(T + 309)
        self.assertEqual(tracker.tick(T + 309), ["picked_up", "state"])

    def test_the_picked_up_op(self):
        tracker = self.ready()
        self.assertEqual(tracker.picked_up("c9", T + 400), [])
        self.assertEqual(tracker.picked_up("c1", T + 400), ["picked_up", "state"])

    def test_lifetimes(self):
        tracker = self.ready()
        self.assertEqual(tracker.tick(T + 235 + 1799), [])
        self.assertEqual(tracker.tick(T + 235 + 1800), ["expired", "state"])
        tracker = self.ready()
        tracker.picked_up("c1", T + 400)
        self.assertEqual(tracker.tick(T + 700), ["expired", "state"])
        tracker = cf.Tracker()
        brewing(tracker)
        tracker.status_idle(T + 100)
        self.assertEqual(tracker.tick(T + 399), [])
        self.assertEqual(tracker.tick(T + 400), ["expired", "state"])


class PowerPathTests(unittest.TestCase):
    def test_a_plug_only_brew_appears_at_the_heat_and_ends_unobserved(self):
        tracker = cf.Tracker()
        self.assertEqual(tracker.power(("grinding", T)), [])
        self.assertEqual(tracker.power(("heating", T, T + 20)), ["started", "bloom", "state", "live"])
        brew = tracker.brew
        self.assertEqual((brew["id"], brew["source"], brew["phase"], brew["started_ts"], brew["bloom_ts"]),
                         ("c1800000000", "power", "brewing", T, T + 20))
        self.assertIsNone(tracker.curve)
        self.assertEqual(tracker.power(("ended", T, T + 175, 7, True)), ["ready", "state", "live"])
        self.assertEqual((tracker.phase(), tracker.brew["outcome"], tracker.brew["ready_ts"]),
                         ("ready", "unobserved", T + 175))

    def test_a_plug_only_brew_that_ended_early_is_stopped_but_ground(self):
        tracker = cf.Tracker()
        tracker.power(("heating", T, T + 20))
        tracker.power(("ended", T, T + 36, 1, False))
        self.assertEqual((tracker.phase(), tracker.brew["ground"], tracker.brew["stop_reason"]),
                         ("stopped", True, "machine"))

    def test_the_plug_never_starts_a_second_brew_beside_a_bluetooth_one(self):
        tracker = cf.Tracker()
        tracker.grinding_seen(T, "machine")
        self.assertEqual(tracker.power(("heating", T, T + 20)), [])
        self.assertEqual(tracker.brew["source"], "machine")

    def test_bluetooth_arriving_during_a_plug_brew_upgrades_its_source(self):
        tracker = cf.Tracker()
        tracker.power(("heating", T, T + 20))
        self.assertEqual(tracker.grinding_seen(T + 30, "machine"), ["source", "state"])
        self.assertEqual(tracker.brew["source"], "machine")

    def test_a_bluetooth_brew_whose_link_went_quiet_is_closed_by_the_plug(self):
        tracker = cf.Tracker()
        brewing(tracker)
        tracker.frame(T + 100)
        tracker.power(("ended", T + 6, T + 200, 7, True))
        self.assertEqual(tracker.phase(), "brewing")
        self.assertNotIn("ready", tracker.tick(T + 300))
        effects = tracker.tick(T + 320)
        self.assertIn("ready", effects)
        self.assertEqual(tracker.brew["ready_ts"], T + 200)
        self.assertNotIn("enjoy_ts", tracker.brew)

    def test_enjoy_on_a_live_link_wins_over_the_plug(self):
        tracker = cf.Tracker()
        brewing(tracker)
        tracker.power(("ended", T + 6, T + 200, 7, True))
        for second in range(200, 260):
            tracker.frame(T + second)
            tracker.tick(T + second)
        self.assertEqual(tracker.phase(), "brewing")
        tracker.enjoy(T + 260)
        self.assertEqual(tracker.brew["enjoy_ts"], T + 260)


class PersistenceTests(unittest.TestCase):
    def test_the_tracker_survives_a_round_trip_mid_brew(self):
        tracker = cf.Tracker()
        brewing(tracker)
        tracker.frame(T + 40)
        tracker.reading_cup(T + 40, 12.0)
        tracker.tick(T + 40)
        again = cf.Tracker.from_dict(json.loads(json.dumps(tracker.to_dict())))
        self.assertEqual(again.to_dict(), tracker.to_dict())
        self.assertEqual(again.phase(), "brewing")

    def test_junk_restores_to_nothing(self):
        self.assertIsNone(cf.Tracker.from_dict(None).brew)
        self.assertIsNone(cf.Tracker.from_dict({"brew": "x", "curve": {"bad": 1}}).brew)


class TimeTests(unittest.TestCase):
    def test_iso_and_epoch(self):
        self.assertEqual(cf.iso(T), "2027-01-15T08:00:00+00:00")
        self.assertEqual(cf.epoch("2027-01-15T08:00:00+00:00"), T)
        self.assertEqual(cf.epoch("2027-01-15T09:00:00+01:00"), T)
        self.assertEqual(cf.epoch(T), T)
        self.assertIsNone(cf.epoch("soon"))
        self.assertIsNone(cf.epoch(True))


if __name__ == "__main__":
    unittest.main()
