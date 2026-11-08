import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cook_publish as pub

NOW = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc).timestamp()


def bath(phase="heating", **extra):
    return {"phase": phase, "temp": 45.2, "target": 58, "ready_at": NOW + 600,
            "last_target": 58, "last_minutes": 150, "rate": 1.28, **extra}


def probe(phase="tracking", **extra):
    return {"phase": phase, "preset": "beef", "serve": 54, "pull": 50, "core": 41.2,
            "ambient": 93.1, "needle": [41.2, 42.3, 44.5, 46.2], "core_index": 0,
            "in_meat": True, "eta": NOW + 800, "slope": 0.66, **extra}


def strings(test, attrs, entity):
    test.assertEqual(attrs["v"], "1")
    for key, value in attrs.items():
        test.assertTrue(pub.leaves_ok(value), (key, value))
    test.assertLessEqual(pub.size(attrs), pub.CAPS[entity])


class EncodingTests(unittest.TestCase):
    def test_numbers_preserve_zero_and_reject_nonfinite(self):
        for value, expected in ((0, "0"), (58, "58"), (54.1, "54.1"), (-0.1, "-0.1")):
            self.assertEqual(pub.num(value, 1), expected)
        for value in (None, True, False, float("nan"), float("inf"), "unknown"):
            self.assertIsNone(pub.num(value, 1))

    def test_local_iso_handles_summer_winter_and_zero(self):
        self.assertEqual(pub.iso(NOW), "2026-11-08T17:00:00+01:00")
        summer = datetime(2026, 7, 8, 16, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(pub.iso(summer), "2026-07-08T18:00:00+02:00")
        self.assertEqual(pub.iso(0), "1970-01-01T01:00:00+01:00")
        self.assertIsNone(pub.iso("unknown"))

    def test_leaves_accept_only_contract_shapes(self):
        for value in ("", "0", [], ["a"], [{"a": "0"}]):
            self.assertTrue(pub.leaves_ok(value))
        for value in (0, False, None, {}, [1], [{"a": 0}], [{"a": []}]):
            self.assertFalse(pub.leaves_ok(value))

    def test_size_counts_utf8_bytes(self):
        self.assertEqual(pub.size({"a": "≈"}), len('{"a":"≈"}'.encode("utf-8")))


class StateTests(unittest.TestCase):
    def test_lane_combination_and_inactive_contract(self):
        for water, roast, expected in (({}, {}, "off"), (bath(), {}, "bath"), ({}, probe(), "probe"),
                                       (bath(), probe(), "both")):
            state, attrs = pub.state_entity(water, roast, NOW)
            self.assertEqual(state, expected)
            strings(self, attrs, "cook_state")
        _, attrs = pub.state_entity({}, {}, NOW)
        self.assertEqual((attrs["bath_phase"], attrs["probe_phase"], attrs["next_kind"], attrs["next_label"]),
                         ("off", "off", "", ""))
        self.assertNotIn("next_at", attrs)
        self.assertNotIn("bath_temp", attrs)

    def test_every_bath_phase_is_encoded(self):
        for phase in ("off", "heating", "too_hot", "ready", "cooking", "done", "chilling", "chilled", "interrupted"):
            water = bath(phase, in_at=NOW - 50, done_at=NOW + 900, chill_until=NOW + 1000,
                         stopped_at=NOW - 10, minutes=150, low_water=True)
            _, attrs = pub.state_entity(water, {}, NOW)
            self.assertEqual(attrs["bath_phase"], phase)
            self.assertEqual(attrs["bath_low_water"], "1")
            strings(self, attrs, "cook_state")

    def test_every_probe_phase_is_encoded(self):
        for phase in ("off", "tracking", "near", "pull", "resting", "carve"):
            roast = probe(phase, pulled_at=NOW - 50, pulled_core=51.2, peak=54.2,
                          carve_at=NOW + 500, stale_since=NOW - 100)
            _, attrs = pub.state_entity({}, roast, NOW)
            self.assertEqual(attrs["probe_phase"], phase)
            self.assertEqual(attrs["probe_needle"], "412,423,445,462")
            self.assertEqual(attrs["probe_core_index"], "0")
            strings(self, attrs, "cook_state")

    def test_exact_state_fields_and_presets(self):
        _, attrs = pub.state_entity(bath("cooking", in_at=NOW, minutes=150, done_at=NOW + 9000,
                                               chill_until=NOW + 9900, stopped_at=NOW),
                                    probe("resting", pulled_at=NOW, pulled_core=50, peak=50,
                                          carve_at=NOW + 900, stale_since=NOW), NOW)
        expected = {"v", "next_kind", "next_at", "next_label", "bath_phase", "bath_temp", "bath_target",
                    "bath_ready_at", "bath_in_at", "bath_minutes", "bath_done_at", "bath_chill_until",
                    "bath_stopped_at", "bath_last_target", "bath_last_minutes", "bath_rate", "probe_phase",
                    "probe_preset", "probe_serve", "probe_pull", "probe_core", "probe_ambient", "probe_needle",
                    "probe_core_index", "probe_in_meat", "probe_eta", "probe_slope", "probe_pulled_at",
                    "probe_pulled_core", "probe_peak", "probe_carve_at", "probe_stale_since", "presets",
                    "friendly_name", "icon"}
        self.assertEqual(set(attrs), expected)
        rows = {row["key"]: row for row in attrs["presets"]}
        self.assertEqual(rows["beef"], {"key": "beef", "serve": "54", "pull": "50", "rest": "15"})
        self.assertEqual(rows["chicken"], {"key": "chicken", "serve": "66", "pull": "64", "rest": "10"})
        for row in rows.values():
            self.assertEqual(set(row), {"key", "serve", "pull", "rest"})

    def test_unknown_values_omitted_false_flags_omitted_and_zero_preserved(self):
        _, attrs = pub.state_entity(bath(temp=0, low_water=False, ready_at=None),
                                    probe(core=0, ambient=None, needle=[0, None], in_meat=False, eta=None), NOW)
        self.assertEqual((attrs["bath_temp"], attrs["probe_core"], attrs["probe_needle"]), ("0", "0", "0,,,"))
        for key in ("bath_low_water", "probe_in_meat", "probe_ambient", "probe_eta", "bath_ready_at"):
            self.assertNotIn(key, attrs)

    def test_alarms_take_priority_over_hands_on_moments(self):
        _, attrs = pub.state_entity(bath("done", low_water=True), probe("pull", lost=True, stale_since=NOW - 100), NOW)
        self.assertEqual(attrs["next_kind"], "low_water")
        _, attrs = pub.state_entity(bath("done"), probe("pull", lost=True, stale_since=NOW - 100), NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("lost", "No reading since 16:58"))
        self.assertEqual(attrs["next_at"], pub.iso(NOW - 100))

    def test_low_water_keeps_inactive_bath_visible(self):
        self.assertEqual(pub.state_entity({"phase": "off", "low_water": True}, {}, NOW)[0], "bath")

    def test_earliest_future_moment_wins_between_lanes(self):
        _, attrs = pub.state_entity(bath(ready_at=NOW + 600), probe(eta=NOW + 500), NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("pull", "Pull ≈ 17:08"))
        _, attrs = pub.state_entity(bath("cooking", done_at=NOW + 300), probe(eta=NOW + 500), NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("bath_done", "Done 17:05"))

    def test_current_actions_take_priority_over_future_predictions(self):
        _, attrs = pub.state_entity(bath("ready"), probe(eta=NOW + 60), NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("bath_ready", "Bag in · bath ready"))
        _, attrs = pub.state_entity(bath("done"), probe("pull", _pull_at=NOW), NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"], attrs["next_at"]),
                         ("pull", "Pull 17:00", pub.iso(NOW)))

    def test_missing_predictions_do_not_invent_an_eta(self):
        _, attrs = pub.state_entity({}, probe(eta=None), NOW)
        self.assertEqual(attrs["next_label"], "Pull")
        self.assertNotIn("next_at", attrs)
        _, attrs = pub.state_entity(bath(ready_at=None), {}, NOW)
        self.assertEqual(attrs["next_label"], "Ready")
        self.assertNotIn("next_at", attrs)

    def test_overshoot_and_interruption_labels(self):
        _, attrs = pub.state_entity(bath("too_hot", temp=61.4), {}, NOW)
        self.assertEqual(attrs["next_label"], "+3.4° · add cold")
        _, attrs = pub.state_entity(bath("interrupted", stopped_at=NOW - 60), {}, NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("", "Stopped 16:59"))

    def test_chill_and_carve_moments(self):
        for phase, label in (("chilling", "Fridge 17:15"), ("chilled", "Into the fridge")):
            _, attrs = pub.state_entity(bath(phase, chill_until=NOW + 900), {}, NOW)
            self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("chill_done", label))
        _, attrs = pub.state_entity({}, probe("resting", carve_at=NOW + 600), NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("carve", "Carve ≈ 17:10"))
        _, attrs = pub.state_entity({}, probe("carve", carve_at=NOW), NOW)
        self.assertEqual((attrs["next_kind"], attrs["next_label"]), ("carve", "Carve 17:00"))
        _, attrs = pub.state_entity({}, probe("carve", carve_at=NOW + 600, _carved_at=NOW), NOW)
        self.assertEqual((attrs["next_at"], attrs["next_label"], attrs["probe_carve_at"]),
                         (pub.iso(NOW), "Carve 17:00", pub.iso(NOW + 600)))


class LiveTests(unittest.TestCase):
    def test_empty_series_still_have_contract_fields(self):
        count, attrs = pub.live_entity([])
        self.assertEqual((count, attrs["step"], attrs["bath"], attrs["core"], attrs["ambient"]),
                         ("0", "30", "", "", ""))
        self.assertNotIn("t0", attrs)
        strings(self, attrs, "cook_live")

    def test_series_align_and_preserve_zero_and_gaps(self):
        samples = [{"at": NOW, "bath": 0, "core": -0.1, "ambient": 20},
                   {"at": NOW + 30, "bath": None, "core": 40.2, "ambient": None},
                   {"at": NOW + 60, "bath": 58.1, "core": None, "ambient": 93.4}]
        count, attrs = pub.live_entity(samples, NOW + 30)
        self.assertEqual((count, attrs["bath"], attrs["core"], attrs["ambient"]),
                         ("3", "0,,581", "-1,402,", "200,,934"))
        self.assertEqual((attrs["t0"], attrs["step"], attrs["pull_mark"]), (pub.iso(NOW), "30", "1"))
        strings(self, attrs, "cook_live")

    def test_four_hour_window_is_bounded(self):
        samples = [{"at": NOW + i * 30, "core": i / 100} for i in range(1000)]
        count, attrs = pub.live_entity(samples)
        self.assertEqual(int(count), 481)
        self.assertEqual(attrs["t0"], pub.iso(NOW + (999 - 480) * 30))
        self.assertEqual(len(attrs["bath"].split(",")), int(count))
        strings(self, attrs, "cook_live")

    def test_worst_case_trims_oldest_rows_and_reindexes_pull(self):
        samples = [{"at": NOW + i * 30, "bath": 999.9, "core": -99.9, "ambient": 999.9} for i in range(481)]
        pulled = NOW + 450 * 30
        count, attrs = pub.live_entity(samples, pulled)
        self.assertLess(int(count), len(samples))
        self.assertEqual(attrs["step"], "30")
        for key in ("bath", "core", "ambient"):
            self.assertEqual(len(attrs[key].split(",")), int(count))
        first = pub.epoch(attrs["t0"])
        self.assertEqual(int(attrs["pull_mark"]), int((pulled - first) / 30))
        self.assertEqual(attrs["t0"], pub.iso(NOW + (len(samples) - int(count)) * 30))
        strings(self, attrs, "cook_live")

    def test_pull_mark_omitted_when_outside_visible_range(self):
        samples = [{"at": NOW + i * 30, "core": 50} for i in range(5)]
        for pulled in (None, NOW - 1, NOW + 150):
            self.assertNotIn("pull_mark", pub.live_entity(samples, pulled)[1])
        self.assertEqual(pub.live_entity(samples, NOW + 149)[1]["pull_mark"], "4")


class LogTests(unittest.TestCase):
    def test_last_twenty_cooks_are_newest_first_and_flat(self):
        records = [{"id": f"p{i}", "kind": "probe", "start": NOW + i, "end": NOW + i + 60,
                    "peak": 54.1, "in_meat": True, "shadow": [{"at": NOW}], "curve": [1, 2],
                    "details": {"a": 1}, "unknown": None} for i in range(30)]
        count, attrs = pub.log_entity(records)
        self.assertEqual(count, "20")
        self.assertEqual([row["id"] for row in attrs["cooks"]], [f"p{i}" for i in range(29, 9, -1)])
        self.assertEqual(attrs["cooks"][0], {"id": "p29", "kind": "probe", "start": pub.iso(NOW + 29),
                                           "end": pub.iso(NOW + 89), "peak": "54.1"})
        strings(self, attrs, "cook_log")

    def test_twenty_finalized_probe_cooks_fit_with_only_summary_fields(self):
        records = []
        for index in range(20):
            roast = probe("carve", pulled_at=NOW + 30, pulled_core=51.2, peak=54.2,
                          carve_at=NOW + 930, stale_since=NOW - 100)
            roast.update(id=f"probe{int(NOW * 1000) + index}", kind="probe", start=NOW + index,
                         end=NOW + 2000 + index, outcome="carve", rest=15)
            records.append(roast)
        count, attrs = pub.log_entity(records)
        self.assertEqual(count, "20")
        self.assertEqual(set(attrs["cooks"][0]), {"id", "kind", "start", "end", "outcome", "preset",
                                                "serve", "pull", "pulled_core", "peak"})
        strings(self, attrs, "cook_log")

    def test_large_ledger_is_capped_by_dropping_oldest(self):
        records = [{"kind": "probe", "start": NOW + i, "end": NOW + i + 60,
                    "outcome": "ø" * 500} for i in range(30)]
        count, attrs = pub.log_entity(records)
        self.assertLess(int(count), 20)
        self.assertEqual(attrs["cooks"][0]["start"], pub.iso(NOW + 29))
        strings(self, attrs, "cook_log")

    def test_empty_and_oversized_single_records_remain_valid(self):
        for records in ([], [{"start": NOW, "outcome": "x" * 10000}]):
            count, attrs = pub.log_entity(records)
            self.assertEqual((count, attrs["cooks"]), ("0", []))
            strings(self, attrs, "cook_log")


if __name__ == "__main__":
    unittest.main()
