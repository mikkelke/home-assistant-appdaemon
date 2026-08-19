# tests/test_dryer_policy_table.py - per-row coverage of dryer_policy.TABLE against the real
# ApplianceFSM (appliance_fsm.py) + real DryerPolicy (dryer_policy.py). Run from repo root:
# python3 -m unittest discover -s apps/appliances/tests -q
#
# Two layers: (1) a coverage meta-test that iterates dryer_policy.TABLE itself and asserts every
# listed (state, evidence) pair has at least one row this file exercises - so a new row added to
# the table without a matching test here fails loudly, per spec 11.1's "drive it from the
# transition-table data structure so new rows auto-require a test"; (2) hand-authored scenarios
# for every semantically distinct guard-branch, asserting internal target, mapped published
# value, whether an action fired, and (for a RESPECT-cooling row) the REFUSED log line.
#
# Only the I/O boundary is faked (FakeClock/FakeScheduler, a dict-backed get_state/get_history,
# a recording ActionSink/log/publish) - dryer_policy.TABLE, DryerPolicy's guards/actions, and
# appliance_fsm.ApplianceFSM are always the real code under test.

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from appliance_fsm import ApplianceFSM, Evidence, EvidenceType, State  # noqa: E402
import dryer_policy as dp  # noqa: E402

E = EvidenceType
S = State
T0 = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

CFG = {
    "power_sensor": "sensor.dryer_plug_power", "energy_sensor": "sensor.dryer_plug_energy",
    "door_sensor": "binary_sensor.dryer_door_contact",
    "start_w": 8, "stop_w": 5, "min_cycle_minutes": 60, "min_energy_kwh": 0.05,
    "fill_window_minutes": 60, "max_running_hours": 5, "pause_timeout_minutes": 10,
    "unemptied_timeout_hours": 24, "emptied_timeout_minutes": 30,
    "restore_corroboration_window_minutes": 10, "announce_freshness_minutes": 20,
}


# --- local injected seams (per-file, not a shared tests/replay.py - see assignment) ---


class FakeClock:
    def __init__(self, t0=T0):
        self._t = t0

    def now(self):
        return self._t

    def advance(self, secs):
        self._t = self._t + timedelta(seconds=secs)


class FakeScheduler:
    def __init__(self, clock):
        self.clock = clock
        self._timers = []
        self._seq = 0

    def run_in(self, delay_s, cb):
        h = self._seq
        self._seq += 1
        self._timers.append([self.clock.now() + timedelta(seconds=delay_s), h, cb, True])
        return h

    def cancel(self, handle):
        for t in self._timers:
            if t[1] == handle:
                t[3] = False

    def advance_to(self, when):
        while True:
            due = sorted([t for t in self._timers if t[3] and t[0] <= when], key=lambda t: (t[0], t[1]))
            if not due:
                break
            t = due[0]
            t[3] = False
            self.clock._t = t[0]
            t[2]()
        self.clock._t = when


class RecordingSink:
    def __init__(self):
        self.announces = []
        self.pushes = []
        self.selects = []
        self.feedbacks = []

    def announce(self, message, **kw):
        self.announces.append(message)

    def push_mobile(self, message, **kw):
        self.pushes.append(message)

    def select_option(self, entity, option, **kw):
        self.selects.append((entity, option))

    def reset_selectors(self, **kw):
        pass

    def save_feedback(self, record):
        self.feedbacks.append(record)


class LogSink:
    def __init__(self):
        self.records = []

    def __call__(self, message, level="INFO"):
        self.records.append((level, message))

    def has(self, needle):
        return any(needle in m for _lv, m in self.records)

    def at(self, level):
        return [m for lv, m in self.records if lv == level]


class Harness:
    """Real DryerPolicy + real ApplianceFSM, fake I/O. `states` is a plain dict this test seeds
    for get_state (power/energy/door/selector entities); `history` is entity -> list[{"state",
    "last_changed"}] for get_history."""

    def __init__(self, *, state=S.OFF, cooling_period=0.0, cfg_overrides=None):
        cfg = {**CFG, **(cfg_overrides or {})}
        self.clock = FakeClock()
        self.sched = FakeScheduler(self.clock)
        self.sink = RecordingSink()
        self.log = LogSink()
        self.states = {CFG["power_sensor"]: "0", CFG["energy_sensor"]: "10.0", CFG["door_sensor"]: "off"}
        self.history = {}
        self.published_calls = []
        self.policy = dp.DryerPolicy(config=cfg)

        def get_state(entity, **kw):
            return self.states.get(entity)

        def get_history(entity, **kw):
            return self.history.get(entity, [])

        def publish(published, *, internal, store_only, attrs):
            # Mirrors dryer_shadow.py's real _publish: EVERY landed transition (store-only or
            # not) passes through sync_watchdogs_on_publish (watchdog cancel-sync + the OFF-
            # landing physics reset) - a Harness that skipped this would test a fictional host,
            # not the one dryer_shadow.py actually is.
            self.policy.sync_watchdogs_on_publish(self.fsm.ctx, internal)
            self.published_calls.append({"published": published, "internal": internal, "store_only": store_only})

        self.fsm = ApplianceFSM(
            self.policy, self.sink, self.clock, self.sched, publish, self.log,
            config=cfg, get_state=get_state, get_history=get_history,
            cooling_period=cooling_period, initial_state=state,
        )

    def ev(self, etype, payload=None, live=True):
        return Evidence.make(etype, self.clock.now(), "test", live=live, payload=payload or {})

    def enter_running(self, watts=600):
        """OFF -> STARTING -> RUNNING via the real table (mints cycle_id, arms watchdog)."""
        self.states[CFG["power_sensor"]] = str(watts)
        self.fsm.submit(self.ev(E.POWER_HIGH, {"watts": watts}))
        self.fsm.submit(self.ev(E.POWER_START_CONFIRMED, {"watts": watts}))

    def enter_ending(self, watts=2):
        self.states[CFG["power_sensor"]] = str(watts)
        self.fsm.submit(self.ev(E.POWER_LOW, {"watts": watts}))

    def past_guard(self, minutes=200):
        self.policy.start_time = self.clock.now() - timedelta(minutes=minutes)

    def clear_cooling(self):
        """Advance the fake clock well past any cooling_period this Harness was built with, so
        a SETUP step (e.g. getting into Paused/Finished before the row under test) is never
        itself blocked by a RESPECT-cooling row it is not the one being tested."""
        self.clock.advance(max(700.0, self.fsm._cooling_period + 100))


# --- (1) coverage meta-test: every (state, evidence) key in TABLE is exercised somewhere below ---


class TableRowCoverage(unittest.TestCase):
    """Fails loudly if dryer_policy.TABLE gains a (state, evidence) pair no test class below
    claims to cover - see EXERCISED below, populated by each scenario class's covers() list."""

    def test_every_table_pair_is_covered(self):
        table_pairs = {
            (state, evtype) for state, by_ev in dp.TABLE.items() for evtype in by_ev
        }
        missing = table_pairs - EXERCISED
        self.assertEqual(
            missing, set(),
            f"dryer_policy.TABLE has (state, evidence) pairs with no test coverage: "
            f"{sorted((s.name, e.name) for s, e in missing)} - add a scenario to "
            f"test_dryer_policy_table.py and register it in EXERCISED",
        )

    def test_row_count_matches_spec_shape(self):
        # Documents the table's size so a silent row deletion/addition is visible in a diff -
        # spec section 4's ~36 canonical rows plus RECONCILE (engine-native, section 8) on
        # RUNNING+PAUSED.
        total = sum(len(rows) for by_ev in dp.TABLE.values() for rows in by_ev.values())
        self.assertEqual(total, 42)


EXERCISED = set()


def covers(*pairs):
    """Class decorator: registers (state, evidence) pairs as covered by this TestCase, for
    TableRowCoverage above."""

    def deco(cls):
        EXERCISED.update(pairs)
        return cls

    return deco


# --- OFF ---


@covers((S.OFF, E.POWER_HIGH), (S.OFF, E.POWER_LOW), (S.OFF, E.DOOR_OPENED), (S.OFF, E.DOOR_CLOSED))
class OffRows(unittest.TestCase):
    def test_power_high_arms_starting_store_only(self):
        h = Harness(state=S.OFF)
        res = h.fsm.submit(h.ev(E.POWER_HIGH, {"watts": 20}))
        self.assertEqual(h.fsm.state, S.STARTING)
        self.assertEqual(h.fsm.published, "Off")
        self.assertTrue(res.store_only)

    def test_power_low_stays_off_noop(self):
        h = Harness(state=S.OFF)
        res = h.fsm.submit(h.ev(E.POWER_LOW, {"watts": 0}))
        self.assertEqual(h.fsm.state, S.OFF)
        # A listed Row() DOES match (its empty guard tuple trivially passes) - it just has no
        # action and no state change, so acted=False and moved=False (the "explicit no-op row"
        # DEBUG case; matched=False is reserved for an UNLISTED pair - see test_fsm_engine.py's
        # own TableInterpreter tests, which pin exactly this distinction at the engine level).
        self.assertTrue(res.matched)
        self.assertFalse(res.published_changed)

    def test_door_events_stay_off(self):
        h = Harness(state=S.OFF)
        h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 0}))
        h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 0}))
        self.assertEqual(h.fsm.state, S.OFF)


# --- STARTING ---


@covers((S.STARTING, E.POWER_START_CONFIRMED), (S.STARTING, E.POWER_LOW), (S.STARTING, E.DOOR_OPENED))
class StartingRows(unittest.TestCase):
    def test_confirmed_with_live_power_mints_cycle_and_goes_running(self):
        h = Harness(state=S.STARTING)
        h.states[CFG["power_sensor"]] = "600"
        res = h.fsm.submit(h.ev(E.POWER_START_CONFIRMED, {"watts": 600}))
        self.assertEqual(h.fsm.state, S.RUNNING)
        self.assertEqual(h.fsm.published, "Running")
        self.assertIsNotNone(h.fsm.cycle_id)
        self.assertTrue(h.policy.watchdogs["running"].armed)

    def test_confirmed_without_live_power_refused_by_guard(self):
        h = Harness(state=S.STARTING)
        h.states[CFG["power_sensor"]] = "0"  # G_power_high_live fails
        res = h.fsm.submit(h.ev(E.POWER_START_CONFIRMED, {"watts": 600}))
        self.assertEqual(h.fsm.state, S.STARTING)
        self.assertFalse(res.matched)

    def test_power_low_cancels_to_off(self):
        h = Harness(state=S.STARTING)
        res = h.fsm.submit(h.ev(E.POWER_LOW, {"watts": 0}))
        self.assertEqual(h.fsm.state, S.OFF)

    def test_door_opened_noop(self):
        h = Harness(state=S.STARTING)
        h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 0}))
        self.assertEqual(h.fsm.state, S.STARTING)


# --- RUNNING ---


@covers(
    (S.RUNNING, E.POWER_LOW), (S.RUNNING, E.KEEP_FRESH), (S.RUNNING, E.POWER_HIGH),
    (S.RUNNING, E.DOOR_OPENED), (S.RUNNING, E.WD_RUNNING), (S.RUNNING, E.PLUG_OUTAGE),
    (S.RUNNING, E.RECONCILE),
)
class RunningRows(unittest.TestCase):
    def test_power_low_enters_ending_store_only(self):
        h = Harness(state=S.OFF)
        h.enter_running()
        res = h.fsm.submit(h.ev(E.POWER_LOW, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.ENDING)
        self.assertEqual(h.fsm.published, "Running")
        self.assertTrue(res.store_only)

    def test_keep_fresh_enters_ending_and_sets_flag(self):
        h = Harness(state=S.OFF)
        h.enter_running()
        h.fsm.submit(h.ev(E.KEEP_FRESH, {"mean": 100, "peak": 200, "stdev": 60}))
        self.assertEqual(h.fsm.state, S.ENDING)
        self.assertTrue(h.policy.keep_fresh_detected)

    def test_power_high_tracks_max_power(self):
        h = Harness(state=S.OFF)
        h.enter_running(watts=600)
        h.fsm.submit(h.ev(E.POWER_HIGH, {"watts": 900}))
        self.assertEqual(h.policy.max_power_w, 900)
        self.assertEqual(h.fsm.state, S.RUNNING)

    def test_door_opened_high_power_pauses(self):
        # cooling_period=600 on purpose: this row is RESPECT (spec 3.3), so the pause-entry must
        # survive a real cooling window, not merely a cooling_period=0 harness.
        h = Harness(state=S.OFF, cooling_period=600)
        h.enter_running()
        h.clear_cooling()
        res = h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 500}))
        self.assertFalse(res.refused)
        self.assertEqual(h.fsm.state, S.PAUSED)
        self.assertTrue(h.policy.watchdogs["pause"].armed)

    def test_door_opened_low_power_before_fill_window_goes_off(self):
        h = Harness(state=S.OFF)
        h.enter_running()
        res = h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 2}))  # elapsed ~0 < fill_window
        self.assertEqual(h.fsm.state, S.OFF)

    def test_door_opened_past_fill_window_invalid_cycle_goes_off(self):
        h = Harness(state=S.OFF, cfg_overrides={"fill_window_minutes": 0, "min_cycle_minutes": 999})
        h.enter_running()
        res = h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 2}))
        self.assertEqual(h.fsm.state, S.OFF)

    def test_door_opened_valid_but_not_past_guard_goes_off(self):
        h = Harness(state=S.OFF, cfg_overrides={"fill_window_minutes": 0, "min_cycle_minutes": 1})
        h.enter_running()
        # run_minutes ~0, guard_dur defaults to 120min fallback -> nowhere near 80%
        res = h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 2}))
        self.assertEqual(h.fsm.state, S.OFF)

    def test_door_opened_valid_and_past_guard_goes_emptying(self):
        h = Harness(state=S.OFF, cfg_overrides={"fill_window_minutes": 0, "min_cycle_minutes": 1})
        h.enter_running()
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)
        res = h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 2}))
        self.assertEqual(h.fsm.state, S.EMPTIED)
        self.assertEqual(len(h.sink.feedbacks), 1)
        self.assertEqual(h.sink.announces, [])  # silent - user was already emptying
        self.assertTrue(h.policy.watchdogs["emptied"].armed)

    def test_wd_running_forces_off_respecting_cooling(self):
        h = Harness(state=S.OFF, cooling_period=600)
        h.enter_running()
        res = h.fsm.submit(h.ev(E.WD_RUNNING))
        self.assertTrue(res.refused)
        self.assertTrue(h.log.has("REFUSED RUNNING->OFF (WD_RUNNING/WATCHDOG)"))
        h.clock.advance(601)
        h.fsm.submit(h.ev(E.WD_RUNNING))
        self.assertEqual(h.fsm.state, S.OFF)

    def test_plug_outage_wipes_to_off(self):
        h = Harness(state=S.OFF)
        h.enter_running()
        h.fsm.submit(h.ev(E.PLUG_OUTAGE))
        self.assertEqual(h.fsm.state, S.OFF)
        self.assertIsNone(h.policy.start_time)

    def test_reconcile_no_op_when_not_hypothesis(self):
        h = Harness(state=S.OFF)
        h.enter_running()
        res = h.fsm.submit(h.ev(E.RECONCILE, live=False))
        self.assertFalse(res.matched)
        self.assertEqual(h.fsm.state, S.RUNNING)


# --- PAUSED ---


@covers(
    (S.PAUSED, E.DOOR_CLOSED), (S.PAUSED, E.WD_PAUSE), (S.PAUSED, E.WD_RUNNING),
    (S.PAUSED, E.DOOR_OPENED), (S.PAUSED, E.RECONCILE),
)
class PausedRows(unittest.TestCase):
    def _to_paused(self, h):
        h.enter_running()
        h.clear_cooling()  # harmless at cooling_period=0; required at 600 (this row is RESPECT)
        h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 500}))
        self.assertEqual(h.fsm.state, S.PAUSED)

    def test_door_closed_high_power_resumes(self):
        h = Harness(state=S.OFF, cooling_period=0)
        self._to_paused(h)
        h.states[CFG["power_sensor"]] = "600"
        res = h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 600}))
        self.assertEqual(h.fsm.state, S.RUNNING)
        self.assertFalse(h.policy.watchdogs["pause"].armed)

    def test_door_closed_low_power_valid_finishes_announced(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        self._to_paused(h)
        h.states[CFG["power_sensor"]] = "2"
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)  # not required by this row but harmless
        res = h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 2}))
        self.assertEqual(h.fsm.state, S.FINISHED)
        self.assertEqual(h.sink.announces, ["Dryer is ready to be emptied"])

    def test_door_closed_low_power_invalid_goes_off(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 999})
        self._to_paused(h)
        h.states[CFG["power_sensor"]] = "2"
        res = h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 2}))
        self.assertEqual(h.fsm.state, S.OFF)

    def test_wd_pause_assumes_emptied_bypasses_cooling(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_paused(h)
        res = h.fsm.submit(h.ev(E.WD_PAUSE))
        self.assertFalse(res.refused)
        self.assertEqual(h.fsm.state, S.OFF)

    def test_wd_running_forces_off_respecting_cooling(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_paused(h)
        res = h.fsm.submit(h.ev(E.WD_RUNNING))
        self.assertTrue(res.refused)

    def test_door_opened_tracks_time_stays_paused(self):
        h = Harness(state=S.OFF, cooling_period=0)
        self._to_paused(h)
        h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 500}))
        self.assertEqual(h.fsm.state, S.PAUSED)

    def test_reconcile_unreachable_normally_since_not_hypothesis(self):
        h = Harness(state=S.OFF, cooling_period=0)
        self._to_paused(h)
        res = h.fsm.submit(h.ev(E.RECONCILE, live=False))
        self.assertFalse(res.matched)
        self.assertEqual(h.fsm.state, S.PAUSED)


# --- ENDING ---


@covers(
    (S.ENDING, E.POWER_END_CONFIRMED), (S.ENDING, E.KEEP_FRESH), (S.ENDING, E.POWER_RECOVERED),
    (S.ENDING, E.DOOR_OPENED), (S.ENDING, E.WD_RUNNING),
)
class EndingRows(unittest.TestCase):
    def _to_ending(self, h, watts=2):
        h.enter_running()
        h.enter_ending(watts=watts)
        self.assertEqual(h.fsm.state, S.ENDING)

    def test_end_confirmed_past_guard_valid_no_door_edge_finishes(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        self._to_ending(h)
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)
        res = h.fsm.submit(h.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.FINISHED)
        self.assertEqual(h.sink.announces, ["Dryer is ready to be emptied"])
        self.assertEqual(len(h.sink.feedbacks), 1)

    def test_end_confirmed_door_edge_routes_emptied_silently(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        self._to_ending(h)
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)
        h.states[CFG["door_sensor"]] = "on"  # live door open right now
        res = h.fsm.submit(h.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.EMPTIED)
        self.assertEqual(h.sink.announces, [])

    def test_end_confirmed_not_past_guard_reverts_to_running(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        self._to_ending(h)
        res = h.fsm.submit(h.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.RUNNING)
        self.assertEqual(h.sink.feedbacks, [])

    def test_end_confirmed_finish_uld_skips_announce(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1, "min_energy_kwh": 0.01})
        h.enter_running()
        h.enter_ending(watts=2)
        h.states[CFG["energy_sensor"]] = "10.02"  # ~0.02 kWh, matches finish_uld
        h.policy.start_time = h.clock.now() - timedelta(minutes=6)
        h.policy.expected_dur_at_start = 5  # finish_uld's own duration -> guard trivially past
        res = h.fsm.submit(h.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.FINISHED)
        self.assertEqual(h.sink.announces, [], "finish_uld (is_real=False) must skip_announce")

    def test_keep_fresh_past_guard_real_finishes_announced(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"min_cycle_minutes": 1})
        self._to_ending(h)
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)
        res = h.fsm.submit(h.ev(E.KEEP_FRESH, {"mean": 100, "peak": 200, "stdev": 60}))
        self.assertEqual(h.fsm.state, S.FINISHED)
        self.assertEqual(h.sink.announces, ["Dryer is ready to be emptied"])

    def test_power_recovered_reverts_to_running(self):
        h = Harness(state=S.OFF, cooling_period=0)
        self._to_ending(h)
        res = h.fsm.submit(h.ev(E.POWER_RECOVERED, {"watts": 400}))
        self.assertEqual(h.fsm.state, S.RUNNING)

    def test_door_opened_routes_same_as_running(self):
        h = Harness(state=S.OFF, cooling_period=0, cfg_overrides={"fill_window_minutes": 0, "min_cycle_minutes": 1})
        self._to_ending(h)
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)
        res = h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 2}))
        self.assertEqual(h.fsm.state, S.EMPTIED)

    def test_wd_running_forces_off(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_ending(h)
        res = h.fsm.submit(h.ev(E.WD_RUNNING))
        self.assertTrue(res.refused)


# --- FINISHED ---


@covers(
    (S.FINISHED, E.DOOR_OPENED), (S.FINISHED, E.FORCE_EMPTIED), (S.FINISHED, E.WD_UNEMPTIED),
    (S.FINISHED, E.POWER_HIGH),
)
class FinishedRows(unittest.TestCase):
    def _to_finished(self, h):
        h.enter_running()
        h.enter_ending()
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)
        h.clear_cooling()  # this row is RESPECT - required whenever cooling_period > 0
        h.fsm.submit(h.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        self.assertEqual(h.fsm.state, S.FINISHED)

    def test_door_opened_goes_emptied_bypasses_cooling(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_finished(h)
        res = h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 0}))
        self.assertFalse(res.refused)
        self.assertEqual(h.fsm.state, S.EMPTIED)
        self.assertTrue(h.policy.watchdogs["emptied"].armed)

    def test_force_emptied_goes_emptied(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_finished(h)
        res = h.fsm.submit(h.ev(E.FORCE_EMPTIED))
        self.assertFalse(res.refused)
        self.assertEqual(h.fsm.state, S.EMPTIED)

    def test_force_emptied_from_other_states_is_noop(self):
        h = Harness(state=S.RUNNING, cooling_period=0)
        res = h.fsm.submit(h.ev(E.FORCE_EMPTIED))
        self.assertFalse(res.matched)
        self.assertEqual(h.fsm.state, S.RUNNING)

    def test_wd_unemptied_assumes_emptied_bypasses_cooling(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_finished(h)
        res = h.fsm.submit(h.ev(E.WD_UNEMPTIED))
        self.assertFalse(res.refused)
        self.assertEqual(h.fsm.state, S.OFF)

    def test_power_high_anti_crease_stays_unemptied(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_finished(h)
        res = h.fsm.submit(h.ev(E.POWER_HIGH, {"watts": 500}))
        self.assertEqual(h.fsm.state, S.FINISHED)
        self.assertEqual(h.fsm.published, "Unemptied")


# --- EMPTIED ---


@covers((S.EMPTIED, E.DOOR_CLOSED), (S.EMPTIED, E.WD_EMPTIED))
class EmptiedRows(unittest.TestCase):
    def _to_emptied(self, h):
        h.enter_running()
        h.enter_ending()
        h.states[CFG["energy_sensor"]] = "10.5"
        h.past_guard(200)
        h.clear_cooling()  # ENDING/POWER_END_CONFIRMED's FINISHED route is RESPECT
        h.fsm.submit(h.ev(E.POWER_END_CONFIRMED, {"watts": 2}))
        h.fsm.submit(h.ev(E.DOOR_OPENED, {"power_w": 0}))  # FINISHED/DOOR_OPENED is BYPASS
        self.assertEqual(h.fsm.state, S.EMPTIED)

    def test_door_closed_goes_off_bypasses_cooling_the_34h_wedge(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_emptied(h)
        res = h.fsm.submit(h.ev(E.DOOR_CLOSED, {"power_w": 0}))
        self.assertFalse(res.refused)
        self.assertEqual(h.fsm.state, S.OFF)
        self.assertFalse(h.log.has("REFUSED EMPTIED->OFF"))

    def test_wd_emptied_missed_close_bypasses_cooling(self):
        h = Harness(state=S.OFF, cooling_period=600)
        self._to_emptied(h)
        res = h.fsm.submit(h.ev(E.WD_EMPTIED))
        self.assertFalse(res.refused)
        self.assertEqual(h.fsm.state, S.OFF)


if __name__ == "__main__":
    unittest.main()
