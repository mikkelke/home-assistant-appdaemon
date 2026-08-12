# tests/test_bed_presence_compare.py - BedPresenceCompare: the promotion evidence
# for the ESPHome bed-presence strip (2026-08-12, zero nights of track record when
# wired in). Covers edge-matching and lag credit, blip and stale-disagreement
# counting, night bookkeeping (a night = overlap with 21:00-09:00), both-occupied
# episode/minute tracking, unavailable-time accrual, restart survival of the
# counters, and the publish contract (every number a string - AppDaemon 4.5.13
# drops attribute values equal to None/False/0 recursively). Same __new__ +
# monkeypatched-callables harness as the other tests here.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

import bed_presence_compare as bpc  # noqa: E402

LEFT_MAT = "binary_sensor.left_bedside"
RIGHT_MAT = "binary_sensor.right_bedside"
LEFT_STRIP = "binary_sensor.bed_presence_6b9c94_bed_occupied_left"
RIGHT_STRIP = "binary_sensor.bed_presence_6b9c94_bed_occupied_right"
BOTH = "binary_sensor.bed_presence_6b9c94_bed_occupied_both"
AGREEMENT = "sensor.bed_presence_agreement"


def make_app(states=None, *, now=None, state_file=None):
    app = bpc.BedPresenceCompare.__new__(bpc.BedPresenceCompare)
    app.pairs = {
        "left": {"mat": LEFT_MAT, "strip": LEFT_STRIP},
        "right": {"mat": RIGHT_MAT, "strip": RIGHT_STRIP},
    }
    app.both_entity = BOTH
    app.publish_entity = AGREEMENT
    app.lag_match_window_seconds = 600
    app.night_window = (dtime(21, 0), dtime(9, 0))
    app.state_file = state_file or os.path.join(
        tempfile.mkdtemp(prefix="bedcmp_test_"), "state.json"
    )
    app.max_night_records = 21
    app.max_disagreement_records = 5
    app.tick_seconds = 60

    app.states = {
        LEFT_MAT: "off",
        RIGHT_MAT: "off",
        LEFT_STRIP: "off",
        RIGHT_STRIP: "off",
        BOTH: "off",
    }
    app.states.update(states or {})

    clock = {"now": now or datetime(2026, 8, 12, 23, 0)}
    app._clock = clock
    app._now_local = lambda: clock["now"]

    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.set_state = MagicMock()
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app._state = app._fresh_state()
    app._episodes = {side: None for side in app.pairs}
    app._last_accrual = None
    return app


def edge(app, entity, side, role, old, new, *, at=None):
    """Drive one state change the way AppDaemon would: store the new state, then
    fire the listener callback."""
    if at is not None:
        app._clock["now"] = at
    app.states[entity] = new
    app._on_pair_change(entity, "state", old, new, {"side": side, "role": role})


def tick(app, at):
    app._clock["now"] = at
    app._tick({})


def warnings(app):
    return [a[0][0] for a in app.log_calls if a[1].get("level") == "WARNING"]


class EdgeMatchingAndLag(unittest.TestCase):
    def test_strip_leads_mat_within_window_is_an_agreed_edge_with_lag(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on", at=t0 + timedelta(seconds=95))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["edges_agreeing"], 1)
        self.assertEqual(totals["strip_led"], 1)
        self.assertEqual(totals["mat_led"], 0)
        self.assertEqual(totals["lag_count"], 1)
        self.assertAlmostEqual(totals["lag_sum_s"], 95.0)
        self.assertAlmostEqual(totals["lag_max_s"], 95.0)
        self.assertEqual(totals["strip_only"], 0)
        self.assertEqual(totals["mat_only"], 0)
        self.assertEqual(warnings(app), [])

    def test_mat_leads_strip_credits_the_mat(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on", at=t0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on",
             at=t0 + timedelta(seconds=30))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["edges_agreeing"], 1)
        self.assertEqual(totals["mat_led"], 1)
        self.assertAlmostEqual(totals["lag_max_s"], 30.0)

    def test_matching_off_edges_agree_too(self):
        app = make_app({LEFT_MAT: "on", LEFT_STRIP: "on"})
        t0 = datetime(2026, 8, 13, 7, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "on", "off", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "on", "off", at=t0 + timedelta(seconds=200))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["edges_agreeing"], 1)
        self.assertEqual(totals["strip_led"], 1)

    def test_sides_are_independent(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, RIGHT_STRIP, "right", "strip", "off", "on", at=t0)
        edge(app, RIGHT_MAT, "right", "mat", "off", "on",
             at=t0 + timedelta(seconds=10))
        self.assertEqual(app._state["totals"]["right"]["edges_agreeing"], 1)
        self.assertEqual(app._state["totals"]["left"]["edges_agreeing"], 0)

    def test_cloud_mat_passing_through_unknown_on_its_way_to_on_still_matches(self):
        # The Withings mats routinely go off->unknown->on. unknown reads as
        # not-on, so no episode churn until the real on arrives.
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "off", "unknown",
             at=t0 + timedelta(seconds=40))
        edge(app, LEFT_MAT, "left", "mat", "unknown", "on",
             at=t0 + timedelta(seconds=80))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["edges_agreeing"], 1)
        self.assertAlmostEqual(totals["lag_max_s"], 80.0)


class DisagreementCounting(unittest.TestCase):
    def test_strip_blip_reverting_counts_strip_only(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_STRIP, "left", "strip", "on", "off",
             at=t0 + timedelta(seconds=20))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["strip_only"], 1)
        self.assertEqual(totals["edges_agreeing"], 0)
        self.assertEqual(totals["lag_count"], 0)
        self.assertEqual(len(warnings(app)), 1)
        record = app._state["recent_disagreements"][-1]
        self.assertEqual(record["side"], "left")
        self.assertEqual(record["kind"], "strip_only")
        self.assertEqual(record["at"], t0.isoformat())
        self.assertEqual(record["duration_s"], "20")

    def test_mat_off_blip_while_both_in_bed_counts_mat_only(self):
        app = make_app({LEFT_MAT: "on", LEFT_STRIP: "on"})
        t0 = datetime(2026, 8, 13, 2, 0, 0)
        edge(app, LEFT_MAT, "left", "mat", "on", "off", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on",
             at=t0 + timedelta(seconds=45))
        self.assertEqual(app._state["totals"]["left"]["mat_only"], 1)

    def test_divergence_past_the_lag_window_is_counted_once_by_the_on_side(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on", at=t0)
        tick(app, t0 + timedelta(seconds=601))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["mat_only"], 1)
        self.assertEqual(len(warnings(app)), 1)
        # Further ticks must not re-count the same open disagreement.
        tick(app, t0 + timedelta(seconds=1200))
        self.assertEqual(totals["mat_only"], 1)
        self.assertEqual(len(warnings(app)), 1)
        # The eventual close must not count it as agreement either, but should
        # amend the record with the final duration.
        edge(app, LEFT_MAT, "left", "mat", "on", "off",
             at=t0 + timedelta(seconds=5400))
        self.assertEqual(totals["edges_agreeing"], 0)
        self.assertEqual(totals["mat_only"], 1)
        record = app._state["recent_disagreements"][-1]
        self.assertEqual(record["duration_s"], "5400")
        self.assertIn("ended after 5400s", record["detail"])

    def test_lost_revert_callback_still_classifies_the_blip_correctly(self):
        # Strip blips on, its off edge callback is LOST, the minutely tick finds
        # the pair agreeing on the ORIGINAL state. Classification goes by final
        # state vs what the opener diverged into - not by which callback closed -
        # so this must still be a strip_only blip, never an agreed edge.
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        app.states[LEFT_STRIP] = "off"  # revert happens, callback never fires
        tick(app, t0 + timedelta(seconds=60))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["strip_only"], 1)
        self.assertEqual(totals["edges_agreeing"], 0)

    def test_last_disagreement_description_is_published(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_STRIP, "left", "strip", "on", "off",
             at=t0 + timedelta(seconds=20))
        self.assertIn("left strip_only", app._state["last_disagreement"])
        self.assertEqual(app._state["last_disagreement_at"], t0.isoformat())

    def test_recent_disagreements_list_is_capped(self):
        app = make_app()
        t = datetime(2026, 8, 12, 23, 0, 0)
        for i in range(8):
            edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t)
            edge(app, LEFT_STRIP, "left", "strip", "on", "off",
                 at=t + timedelta(seconds=5))
            t += timedelta(minutes=5)
        self.assertEqual(len(app._state["recent_disagreements"]), 5)
        self.assertEqual(app._state["totals"]["left"]["strip_only"], 8)


class RestartDivergence(unittest.TestCase):
    def test_opener_unknown_resolution_counts_agreement_without_lag_sample(self):
        # Mid-divergence restart: the episode's opener is unknown, so the pair
        # agreeing later is an agreed edge but never a lag sample.
        app = make_app({LEFT_STRIP: "on"})
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        app._clock["now"] = t0
        app._compare("left", t0, changed_role=None)  # what initialize() does
        self.assertIsNotNone(app._episodes["left"])
        edge(app, LEFT_MAT, "left", "mat", "off", "on",
             at=t0 + timedelta(seconds=120))
        totals = app._state["totals"]["left"]
        self.assertEqual(totals["edges_agreeing"], 1)
        self.assertEqual(totals["lag_count"], 0)
        self.assertEqual(totals["strip_led"], 0)
        self.assertEqual(totals["mat_led"], 0)


class NightBookkeeping(unittest.TestCase):
    def test_night_key_maps_the_window_to_the_starting_date(self):
        app = make_app()
        self.assertEqual(app._night_key(datetime(2026, 8, 12, 22, 0)), "2026-08-12")
        self.assertEqual(app._night_key(datetime(2026, 8, 13, 3, 0)), "2026-08-12")
        self.assertEqual(app._night_key(datetime(2026, 8, 13, 8, 59)), "2026-08-12")
        self.assertIsNone(app._night_key(datetime(2026, 8, 13, 9, 0)))
        self.assertIsNone(app._night_key(datetime(2026, 8, 13, 15, 0)))

    def test_nights_observed_counts_each_watched_night_once(self):
        app = make_app()
        tick(app, datetime(2026, 8, 12, 22, 0))
        self.assertEqual(app._state["nights_observed"], 1)
        tick(app, datetime(2026, 8, 13, 2, 0))   # same night across midnight
        self.assertEqual(app._state["nights_observed"], 1)
        tick(app, datetime(2026, 8, 13, 12, 0))  # daytime: no night open
        tick(app, datetime(2026, 8, 13, 21, 30))
        self.assertEqual(app._state["nights_observed"], 2)
        self.assertEqual(len(app._state["nights"]), 2)
        self.assertEqual(app._state["nights"][0]["night"], "2026-08-12")
        self.assertEqual(app._state["nights"][1]["night"], "2026-08-13")

    def test_counters_land_in_the_current_night_record(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on",
             at=t0 + timedelta(seconds=60))
        night = app._state["nights"][-1]
        self.assertEqual(night["night"], "2026-08-12")
        self.assertEqual(night["left_edges_agreeing"], 1)
        self.assertEqual(night["left_strip_led"], 1)
        self.assertAlmostEqual(night["left_lag_max_s"], 60.0)

    def test_daytime_counters_do_not_touch_night_records(self):
        app = make_app(now=datetime(2026, 8, 12, 14, 0))
        t0 = datetime(2026, 8, 12, 14, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_STRIP, "left", "strip", "on", "off",
             at=t0 + timedelta(seconds=10))
        self.assertEqual(app._state["totals"]["left"]["strip_only"], 1)
        self.assertEqual(app._state["nights"], [])

    def test_the_morning_closes_the_night_so_daytime_blips_stay_out_of_it(self):
        # 23:00 agreement lands in the night; a 12:00 blip the next day must not
        # be credited to that finished night record.
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on",
             at=t0 + timedelta(seconds=30))
        t1 = datetime(2026, 8, 13, 12, 0, 0)
        edge(app, LEFT_MAT, "left", "mat", "on", "off", at=t1)
        edge(app, LEFT_MAT, "left", "mat", "off", "on",
             at=t1 + timedelta(seconds=10))  # mat blip at noon
        night = app._state["nights"][-1]
        self.assertEqual(night["night"], "2026-08-12")
        self.assertEqual(night["left_mat_only"], 0)
        self.assertEqual(app._state["totals"]["left"]["mat_only"], 1)
        self.assertIsNone(app._state["current_night"])


class BothOccupiedTracking(unittest.TestCase):
    def test_both_edges_count_episodes_per_night_and_total(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 22, 0)
        app._clock["now"] = t0
        app.states[BOTH] = "on"
        app._on_both_change(BOTH, "state", "off", "on", {})
        self.assertEqual(app._state["both"]["episodes_total"], 1)
        self.assertEqual(app._state["nights"][-1]["both_episodes"], 1)

    def test_tick_accrual_adds_both_seconds_to_night_and_total(self):
        app = make_app({BOTH: "on"})
        tick(app, datetime(2026, 8, 12, 22, 0))   # first tick only sets the anchor
        tick(app, datetime(2026, 8, 12, 22, 1))
        tick(app, datetime(2026, 8, 12, 22, 2))
        self.assertAlmostEqual(app._state["both"]["seconds_total"], 120.0)
        self.assertAlmostEqual(app._state["nights"][-1]["both_seconds"], 120.0)

    def test_a_scheduler_gap_cannot_credit_hours_at_once(self):
        app = make_app({BOTH: "on"})
        tick(app, datetime(2026, 8, 12, 22, 0))
        tick(app, datetime(2026, 8, 12, 23, 30))  # 90 min gap, capped at 5 ticks
        self.assertAlmostEqual(app._state["both"]["seconds_total"], 300.0)


class UnavailableAccrual(unittest.TestCase):
    def test_unreadable_strip_accrues_downtime_in_the_night_record(self):
        app = make_app({LEFT_STRIP: "unavailable"})
        tick(app, datetime(2026, 8, 12, 22, 0))
        tick(app, datetime(2026, 8, 12, 22, 1))
        night = app._state["nights"][-1]
        self.assertAlmostEqual(night["left_strip_unavailable_s"], 60.0)
        self.assertAlmostEqual(night["left_mat_unavailable_s"], 0.0)

    def test_unavailable_strip_with_mat_on_becomes_a_mat_only_disagreement(self):
        # A dead witness must not silently look like agreement: the mat seeing
        # someone the strip cannot confirm is exactly a mat_only count, with the
        # downtime attributing the cause.
        app = make_app({LEFT_STRIP: "unavailable"})
        t0 = datetime(2026, 8, 12, 23, 0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on", at=t0)
        tick(app, t0 + timedelta(seconds=601))
        self.assertEqual(app._state["totals"]["left"]["mat_only"], 1)


class RestartSurvival(unittest.TestCase):
    def test_counters_and_nights_round_trip_through_the_state_file(self):
        state_file = os.path.join(tempfile.mkdtemp(prefix="bedcmp_rt_"), "s.json")
        app = make_app(state_file=state_file)
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on",
             at=t0 + timedelta(seconds=95))
        app._save_state()

        reborn = make_app(state_file=state_file)
        reborn._state = reborn._load_state()
        self.assertEqual(reborn._state["totals"]["left"]["edges_agreeing"], 1)
        self.assertEqual(reborn._state["nights_observed"], 1)
        self.assertEqual(reborn._state["nights"][-1]["night"], "2026-08-12")

    def test_version_mismatch_starts_fresh(self):
        state_file = os.path.join(tempfile.mkdtemp(prefix="bedcmp_v_"), "s.json")
        with open(state_file, "w") as f:
            json.dump({"version": 999, "nights_observed": 42}, f)
        app = make_app(state_file=state_file)
        self.assertEqual(app._load_state()["nights_observed"], 0)

    def test_missing_file_starts_fresh(self):
        app = make_app(state_file="/nonexistent/dir/state.json")
        state = app._load_state()
        self.assertEqual(state["nights_observed"], 0)
        self.assertIn("left", state["totals"])


class PublishContract(unittest.TestCase):
    """AppDaemon 4.5.13 drops attribute values equal to None/False/0 recursively
    (utils.remove_literals) - so 0, the most meaningful value this sensor ever
    reports, must always travel as a string."""

    @staticmethod
    def _assert_no_droppable(value, path="attributes"):
        # Anything that == None/False/0 anywhere in the structure would vanish.
        if isinstance(value, dict):
            for k, v in value.items():
                PublishContract._assert_no_droppable(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                PublishContract._assert_no_droppable(v, f"{path}[{i}]")
        else:
            assert isinstance(value, str), f"{path} is {value!r}, not a string"

    def test_fresh_sensor_publishes_all_zeroes_as_strings(self):
        app = make_app()
        app._publish(datetime(2026, 8, 12, 23, 0))
        app.set_state.assert_called_once()
        args, kwargs = app.set_state.call_args
        self.assertEqual(args[0], AGREEMENT)
        self.assertEqual(kwargs["state"], "0")
        self.assertTrue(kwargs["replace"])
        attrs = kwargs["attributes"]
        self._assert_no_droppable(attrs)
        self.assertEqual(attrs["left_edges_agreeing"], "0")
        self.assertEqual(attrs["left_strip_only"], "0")
        self.assertEqual(attrs["left_mat_only"], "0")
        self.assertEqual(attrs["left_lag_avg_s"], "")
        self.assertEqual(attrs["nights_observed"], "0")
        self.assertEqual(attrs["both_episodes_total"], "0")

    def test_populated_sensor_stays_all_strings_including_night_records(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_STRIP, "left", "strip", "off", "on", at=t0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on",
             at=t0 + timedelta(seconds=95))
        edge(app, RIGHT_STRIP, "right", "strip", "off", "on",
             at=t0 + timedelta(seconds=100))
        edge(app, RIGHT_STRIP, "right", "strip", "on", "off",
             at=t0 + timedelta(seconds=110))
        app.set_state.reset_mock()
        app._publish(t0 + timedelta(seconds=120))
        _, kwargs = app.set_state.call_args
        attrs = kwargs["attributes"]
        self._assert_no_droppable(attrs)
        self.assertEqual(kwargs["state"], "1")  # one night observed
        self.assertEqual(attrs["left_edges_agreeing"], "1")
        self.assertEqual(attrs["left_strip_led"], "1")
        self.assertEqual(attrs["left_lag_avg_s"], "95.0")
        self.assertEqual(attrs["right_strip_only"], "1")
        self.assertEqual(attrs["nights"][-1]["left_edges_agreeing"], "1")
        self.assertIn("right strip_only", attrs["last_disagreement"])
        self.assertEqual(
            attrs["left_entities"], f"{LEFT_STRIP} vs {LEFT_MAT}"
        )

    def test_open_disagreement_is_visible_while_it_runs(self):
        app = make_app()
        t0 = datetime(2026, 8, 12, 23, 0, 0)
        edge(app, LEFT_MAT, "left", "mat", "off", "on", at=t0)
        _, kwargs = app.set_state.call_args
        self.assertEqual(
            kwargs["attributes"]["left_open_disagreement_since"], t0.isoformat()
        )


if __name__ == "__main__":
    unittest.main()
