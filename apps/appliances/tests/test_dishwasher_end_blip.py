# tests/test_dishwasher_end_blip.py - the machine's own programme-end blip finishes the cycle
# ahead of the dry tail.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# ECO ends with a short draw at the machine's real end, ahead of the tail timer (guard-open +
# dry_tail_minutes). For a programme with `end_blip: true`, once the guards are met and the dry
# tail is pending, a reading above stop_w followed by stop_for seconds at or below stop_w
# finishes the cycle at once; with no blip the tail timer ends it as before.
#
# Pinned here: the blip finishes after the quiet; no blip -> tail timer; a blip before the guard
# opens is ignored; power rising again restarts the quiet; a programme without the flag (and main
# wash activity on one with it) keeps the old cancel-on-power behaviour; nothing finishes twice;
# a restart mid-tail re-arms the rule; and a real recorded ECO trace records the same duration
# with or without the rule while finishing sooner.
#
# The real methods run (_power_changed, _confirm_finished, _finish_with_dry_tail,
# _dry_tail_elapsed, _transition_to_unemptied, _handle_door_opened); only the AppDaemon I/O
# boundary is faked, with test_dishwasher_dry_tail.py's harness.

from __future__ import annotations

import heapq
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_dishwasher_dry_tail import (  # noqa: E402  (installs the appdaemon stub)
    ECO_TAIL_TARGET,
    START,
    _TEST_PROFILES,
    dm,
    make_running_app,
)
from test_dishwasher_audit_2026_09 import (  # noqa: E402  (real initialize() harness)
    ANNOUNCE as AUDIT_ANNOUNCE,
    POWER_SENSOR as BOOT_POWER_SENSOR,
    PRODUCTION_ARGS,
    cs,
    load_feedback_cycles,
    make_boot_app,
    scheduled_callbacks_named,
    seed_store,
)

UTC = timezone.utc
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dishwasher_eco_2026_10_11.json"
PROGRAMMES_YAML = Path(__file__).resolve().parents[1] / "dishwasher_programmes.yaml"

T_GUARDS_MET = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)  # guard opened 14:55:10, plug quiet
T_BLIP = datetime(2026, 8, 12, 15, 20, 0, tzinfo=UTC)  # 221.7 min into the run, tail target 15:30:10
ANNOUNCEMENT = "Dishwasher is ready to be emptied"

BLIP_PROFILES = {
    **{k: dict(v) for k, v in _TEST_PROFILES.items()},
    "eco": {**_TEST_PROFILES["eco"], "end_blip": True},
}


def power(app, watts):
    """One plug reading arriving now - the call AppDaemon's listen_state makes."""
    old = app.states[app.power_sensor]
    app.states[app.power_sensor] = str(watts)
    app._power_changed(app.power_sensor, "state", old, str(watts), {})


def scheduled(app, name):
    return [(cb, delay) for cb, delay, _kw in app.scheduled if getattr(cb, "__name__", "") == name]


def log_lines(app):
    return [str(a[0]) for a, _kw in app.log_calls if a]


def tail_pending_app(profiles=BLIP_PROFILES):
    """ECO with the guards met and the plug quiet: the dry tail is pending."""
    dm.DishwasherMonitor.PROGRAMME_PROFILES = {k: dict(v) for k, v in profiles.items()}
    app = make_running_app(T_GUARDS_MET)
    app._confirm_finished({})
    assert app._dry_tail_pending is not None
    return app


class BlipTestCase(unittest.TestCase):
    profiles = BLIP_PROFILES

    def setUp(self):
        self._orig_profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES
        dm.DishwasherMonitor.PROGRAMME_PROFILES = {k: dict(v) for k, v in self.profiles.items()}

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig_profiles


class BlipFinishesAfterQuiet(BlipTestCase):
    def _blip_then_quiet(self, app):
        app.now = T_BLIP
        power(app, 19.0)
        app.now = T_BLIP + timedelta(seconds=10)
        power(app, 0.0)

    def test_blip_followed_by_90s_quiet_finishes_now(self):
        app = tail_pending_app()
        tail_handle = app.dry_tail_timer
        self.assertTrue(app._dry_tail_pending["end_blip"])

        app.now = T_BLIP
        power(app, 19.0)
        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.dry_tail_timer, tail_handle, "the tail stays armed as the fallback")
        self.assertNotIn(tail_handle, app.canceled_timers)
        self.assertTrue(app._dry_tail_pending["blip_seen"])
        self.assertEqual(app.sonos_notifier.calls, [])

        app.now = T_BLIP + timedelta(seconds=10)
        power(app, 0.0)
        quiet = scheduled(app, "_confirm_finished")[-1]
        self.assertEqual(quiet[1], 90)
        self.assertEqual(app.states[app.state_entity], "Running")

        app.now = T_BLIP + timedelta(seconds=100)
        quiet[0]({})

        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, [ANNOUNCEMENT])
        self.assertIsNone(app.dry_tail_timer)
        self.assertIsNone(app._dry_tail_pending)
        self.assertIn(tail_handle, app.canceled_timers)

    def test_log_lines_say_when_the_rule_armed_and_fired(self):
        app = tail_pending_app()
        self._blip_then_quiet(app)
        app.now = T_BLIP + timedelta(seconds=100)
        scheduled(app, "_confirm_finished")[-1][0]({})

        lines = log_lines(app)
        self.assertIn("End blip: 19.0W during the dry tail - the finish follows 90s of quiet", lines)
        self.assertIn(
            "End blip: 90s quiet after the machine's end blip - finishing 8 min ahead of the dry-tail target",
            lines,
        )

    def test_feedback_records_the_run_time_at_the_blip_not_guard_time_or_tail(self):
        """A blip cycle records the run time of the post-blip evaluation. Recording guard time
        (198 min) would lower every learned average, and with it the next guard."""
        app = tail_pending_app()
        with tempfile.TemporaryDirectory() as tmp:
            app.feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
            self._blip_then_quiet(app)
            app.now = T_BLIP + timedelta(seconds=100)
            scheduled(app, "_confirm_finished")[-1][0]({})
            with open(app.feedback_file) as f:
                cycles = json.load(f)["cycles"]

        self.assertEqual(len(cycles), 1)
        blip_run = (T_BLIP + timedelta(seconds=100) - START).total_seconds() / 60  # ~223.4
        guard_run = (T_GUARDS_MET - START).total_seconds() / 60  # ~198.4
        tail_run = (ECO_TAIL_TARGET - START).total_seconds() / 60  # ~231.9
        self.assertAlmostEqual(cycles[0]["duration_min"], blip_run, delta=0.2)
        self.assertGreater(cycles[0]["duration_min"], guard_run + 20)
        self.assertLess(cycles[0]["duration_min"], tail_run - 5)
        self.assertEqual(cycles[0]["end_reason"], "low_power_detected")
        self.assertEqual(cycles[0]["confirmed"], "eco")

    def test_power_back_up_during_the_quiet_restarts_it(self):
        app = tail_pending_app()
        tail_handle = app.dry_tail_timer
        self._blip_then_quiet(app)
        first_quiet = scheduled(app, "_confirm_finished")[-1]
        first_handle = app.low_power_timer

        app.now = T_BLIP + timedelta(seconds=40)
        power(app, 19.0)
        self.assertIn(first_handle, app.canceled_timers)
        self.assertIsNone(app.low_power_timer)
        self.assertEqual(app.states[app.state_entity], "Running")

        # The cancelled timer's callback, were it to run anyway, sees power and finishes nothing.
        app.now = T_BLIP + timedelta(seconds=100)
        first_quiet[0]({})
        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.sonos_notifier.calls, [])
        self.assertEqual(app.dry_tail_timer, tail_handle)

        app.now = T_BLIP + timedelta(seconds=110)
        power(app, 0.0)
        second_quiet = scheduled(app, "_confirm_finished")[-1]
        self.assertEqual(second_quiet[1], 90)
        self.assertEqual(len(scheduled(app, "_confirm_finished")), 2)
        app.now = T_BLIP + timedelta(seconds=200)
        second_quiet[0]({})

        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, [ANNOUNCEMENT])

    def test_blip_below_start_w_counts(self):
        """The blip can sit below start_w; start_w plays no part in it."""
        app = tail_pending_app()
        app.now = T_BLIP
        power(app, 7.0)
        app.now = T_BLIP + timedelta(seconds=5)
        power(app, 0.4)
        app.now = T_BLIP + timedelta(seconds=95)
        scheduled(app, "_confirm_finished")[-1][0]({})
        self.assertEqual(app.states[app.state_entity], "Unemptied")


class NoBlipTheTailTimerFinishes(BlipTestCase):
    def test_quiet_polling_never_finishes_early_and_the_tail_timer_still_does(self):
        app = tail_pending_app()
        tail_handle = app.dry_tail_timer
        pending = app._dry_tail_pending

        for minute in range(1, 30):
            app.now = T_GUARDS_MET + timedelta(minutes=minute)
            power(app, 1.2)
            app._confirm_finished({})  # the poll re-arms the quiet timer every ~100 s
        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.sonos_notifier.calls, [])
        self.assertIs(app._dry_tail_pending, pending)
        self.assertNotIn("blip_seen", pending)
        self.assertEqual(len(scheduled(app, "_dry_tail_elapsed")), 1)

        app.now = ECO_TAIL_TARGET
        app._dry_tail_elapsed({})
        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, [ANNOUNCEMENT])
        self.assertIsNone(app.dry_tail_timer)
        self.assertNotIn(tail_handle, app.canceled_timers)

    def test_tail_timer_records_the_guard_time_run_minutes(self):
        app = tail_pending_app()
        with tempfile.TemporaryDirectory() as tmp:
            app.feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
            app.now = ECO_TAIL_TARGET
            app._dry_tail_elapsed({})
            with open(app.feedback_file) as f:
                cycles = json.load(f)["cycles"]
        guard_run = (T_GUARDS_MET - START).total_seconds() / 60
        self.assertAlmostEqual(cycles[-1]["duration_min"], guard_run, delta=0.2)


class BlipBeforeGuardOpenIsIgnored(BlipTestCase):
    def test_blip_in_the_wash_or_early_dry_does_not_arm_or_finish(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = {k: dict(v) for k, v in BLIP_PROFILES.items()}
        early = START + timedelta(minutes=150)  # guard opens at 196.9 min
        app = make_running_app(early)
        power(app, 0.0)

        app.now = early + timedelta(seconds=30)
        power(app, 19.0)
        app.now = early + timedelta(seconds=40)
        power(app, 0.0)
        app.now = early + timedelta(seconds=130)
        scheduled(app, "_confirm_finished")[-1][0]({})

        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertIsNone(app._dry_tail_pending)
        self.assertIsNone(app.dry_tail_timer)
        self.assertEqual(app.sonos_notifier.calls, [])

        # Past the guard the tail arms fresh, with no blip remembered from before it.
        app.now = T_GUARDS_MET
        app._confirm_finished({})
        self.assertIsNotNone(app._dry_tail_pending)
        self.assertNotIn("blip_seen", app._dry_tail_pending)
        self.assertEqual(app.states[app.state_entity], "Running")

    def test_blip_right_before_the_tail_arms_does_not_finish(self):
        """The blip lands before any quiet confirmed the guards (no tail pending yet): the plug
        draw cancels the quiet timer and nothing is finished until the guards are met."""
        dm.DishwasherMonitor.PROGRAMME_PROFILES = {k: dict(v) for k, v in BLIP_PROFILES.items()}
        app = make_running_app(START + timedelta(minutes=196))
        power(app, 19.0)
        app.now += timedelta(seconds=10)
        power(app, 0.0)
        self.assertIsNone(app._dry_tail_pending)
        self.assertEqual(app.states[app.state_entity], "Running")


class ProgrammeWithoutEndBlipIsUnaffected(BlipTestCase):
    profiles = _TEST_PROFILES

    def test_any_power_during_the_tail_still_drops_it(self):
        app = tail_pending_app(_TEST_PROFILES)
        tail_handle = app.dry_tail_timer
        self.assertFalse(app._dry_tail_pending["end_blip"])

        app.now = T_BLIP
        power(app, 19.0)
        self.assertIsNone(app.dry_tail_timer)
        self.assertIsNone(app._dry_tail_pending)
        self.assertIn(tail_handle, app.canceled_timers)

        app.now = T_BLIP + timedelta(seconds=10)
        power(app, 0.0)
        app.now = T_BLIP + timedelta(seconds=100)
        scheduled(app, "_confirm_finished")[-1][0]({})

        # Re-evaluated after the blip, deferred again to the same tail target; nothing early.
        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.sonos_notifier.calls, [])
        self.assertEqual(app._dry_tail_pending["target"], ECO_TAIL_TARGET)
        self.assertEqual(len(scheduled(app, "_dry_tail_elapsed")), 2)

    def test_quick_stays_off_while_eco_is_on(self):
        profiles = {k: dict(v) for k, v in BLIP_PROFILES.items()}
        profiles["quick"]["dry_tail_minutes"] = 33
        app = tail_pending_app(profiles)
        self.assertTrue(app._dry_tail_pending["end_blip"])
        self.assertFalse(bool(profiles["quick"].get("end_blip")))


class MainActivityStillDropsTheTail(BlipTestCase):
    def test_wash_power_during_the_tail_cancels_it_even_with_end_blip(self):
        """>= 50 W is the wash itself (guards premature), not the end blip: detection restarts."""
        app = tail_pending_app()
        tail_handle = app.dry_tail_timer

        app.now = T_BLIP
        power(app, 300.0)

        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertIsNone(app.dry_tail_timer)
        self.assertIsNone(app._dry_tail_pending)
        self.assertIn(tail_handle, app.canceled_timers)
        self.assertEqual(app.sonos_notifier.calls, [])

    def test_wash_power_after_a_blip_cancels_it_too(self):
        app = tail_pending_app()
        app.now = T_BLIP
        power(app, 19.0)
        app.now = T_BLIP + timedelta(seconds=20)
        power(app, 1800.0)

        self.assertIsNone(app._dry_tail_pending)
        self.assertIsNone(app.dry_tail_timer)
        self.assertEqual(app.states[app.state_entity], "Running")


class NothingFinishesTwice(BlipTestCase):
    def test_stale_callbacks_after_the_blip_finish_are_noops(self):
        app = tail_pending_app()
        with tempfile.TemporaryDirectory() as tmp:
            app.feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
            app.now = T_BLIP
            power(app, 19.0)
            app.now = T_BLIP + timedelta(seconds=10)
            power(app, 0.0)
            quiet = scheduled(app, "_confirm_finished")[-1][0]
            app.now = T_BLIP + timedelta(seconds=100)
            quiet({})
            self.assertEqual(app.states[app.state_entity], "Unemptied")

            quiet({})
            app._confirm_finished({})
            app.now = ECO_TAIL_TARGET
            app._dry_tail_elapsed({})

            with open(app.feedback_file) as f:
                cycles = json.load(f)["cycles"]
        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, [ANNOUNCEMENT])
        self.assertEqual(len(cycles), 1)

    def test_door_opened_after_the_blip_wins_and_the_quiet_does_nothing(self):
        app = tail_pending_app()
        with tempfile.TemporaryDirectory() as tmp:
            app.feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
            app.now = T_BLIP
            power(app, 19.0)
            app.now = T_BLIP + timedelta(seconds=10)
            power(app, 0.0)
            quiet = scheduled(app, "_confirm_finished")[-1][0]

            app.now = T_BLIP + timedelta(seconds=40)
            app.states[app.door_sensor] = "on"
            app._handle_door_opened("Running")
            self.assertEqual(app.states[app.state_entity], "Emptied")
            self.assertIsNone(app._dry_tail_pending)

            app.now = T_BLIP + timedelta(seconds=100)
            quiet({})
            with open(app.feedback_file) as f:
                cycles = json.load(f)["cycles"]

        self.assertEqual(app.states[app.state_entity], "Emptied")
        self.assertEqual(app.sonos_notifier.calls, [])
        self.assertEqual(len(cycles), 1)

    def test_transition_refused_by_cooling_keeps_the_tail_and_retries(self):
        app = tail_pending_app()
        app.last_state_change = T_BLIP - timedelta(seconds=30)  # inside cooling_period
        app.now = T_BLIP
        power(app, 19.0)
        app.now = T_BLIP + timedelta(seconds=10)
        power(app, 0.0)
        app.now = T_BLIP + timedelta(seconds=100)
        scheduled(app, "_confirm_finished")[-1][0]({})

        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertEqual(app.sonos_notifier.calls, [])
        self.assertIsNotNone(app._dry_tail_pending, "the tail is still the fallback")
        self.assertTrue(app._dry_tail_pending["blip_seen"])

        app.last_state_change = None
        app.now = T_BLIP + timedelta(seconds=200)
        app._confirm_finished({})
        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertEqual(app.sonos_notifier.calls, [ANNOUNCEMENT])


class RestartMidTail(BlipTestCase):
    def test_boot_self_heal_deferral_is_armed_for_the_blip(self):
        """A restart inside the tail re-creates the deferral from the restored clock; the rule
        has no memory of a blip before the restart, but a blip after it finishes the cycle."""
        app = make_running_app(START + timedelta(minutes=205))
        app.announce_freshness_minutes = 20
        run_min = 205.0
        app._finish_with_dry_tail(
            classified="eco",
            display_prog="eco",
            guard_dur=214,
            run_minutes=run_min,
            duration_source=None,
            idle_min=None,
            energy_used=0.65,
            end_reason="low_power_detected",
            boot_self_heal=True,
        )
        self.assertEqual(app.states[app.state_entity], "Running")
        self.assertTrue(app._dry_tail_pending["end_blip"])
        self.assertNotIn("blip_seen", app._dry_tail_pending)

        app.now = T_BLIP
        power(app, 19.0)
        app.now = T_BLIP + timedelta(seconds=10)
        power(app, 0.0)
        app.now = T_BLIP + timedelta(seconds=100)
        scheduled(app, "_confirm_finished")[-1][0]({})
        self.assertEqual(app.states[app.state_entity], "Unemptied")

    def test_restart_inside_the_tail_through_initialize_still_finishes_on_a_blip(self):
        """Real initialize() 15 min into the tail with the checked-in programmes: the restored
        tail is pending, and a blip after the restart finishes the cycle."""
        dm.DishwasherMonitor.PROGRAMME_PROFILES = {}
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            restart_elapsed_min = 234 * 0.95 + 15
            app, entities = make_boot_app(
                tmp, helper_state=None, power_w="0", energy_kwh="10.0", extra_args=PRODUCTION_ARGS
            )
            seed_store(tmp, {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=restart_elapsed_min)),
                "energy_at_start": "9.15",
                "detected_programme": "eco",
                "expected_dur_at_start": 234,
                "max_power_w": 1800.0,
                "last_high_power_time": cs.format_utc(now - timedelta(minutes=restart_elapsed_min - 150)),
                "notification_sent": False,
            })
            app.initialize()
            self.assertEqual(app.state, "Running")
            self.assertEqual(len(scheduled_callbacks_named(app, "_dry_tail_elapsed")), 1)
            self.assertTrue(app._dry_tail_pending["end_blip"])

            entities[BOOT_POWER_SENSOR]["state"] = "19.0"
            app._power_changed(BOOT_POWER_SENSOR, "state", "0", "19.0", {})
            entities[BOOT_POWER_SENSOR]["state"] = "0"
            app._power_changed(BOOT_POWER_SENSOR, "state", "19.0", "0", {})
            scheduled_callbacks_named(app, "_confirm_finished")[-1]({})

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(app.sonos_calls, [AUDIT_ANNOUNCE])
            self.assertEqual(len(load_feedback_cycles(app)), 1)

    def test_restart_without_a_blip_falls_back_to_the_tail(self):
        app = make_running_app(START + timedelta(minutes=205))
        app.announce_freshness_minutes = 20
        app._finish_with_dry_tail(
            classified="eco", display_prog="eco", guard_dur=214, run_minutes=205.0,
            duration_source=None, idle_min=None, energy_used=0.65,
            end_reason="low_power_detected", boot_self_heal=True,
        )
        app.now = ECO_TAIL_TARGET
        app._dry_tail_elapsed({})
        self.assertEqual(app.states[app.state_entity], "Unemptied")


class ProgrammeConfig(unittest.TestCase):
    def test_checked_in_yaml_opts_eco_in_and_nothing_else(self):
        import yaml

        with open(PROGRAMMES_YAML) as f:
            programmes = yaml.safe_load(f)["programmes"]
        self.assertIs(programmes["eco"]["end_blip"], True)
        self.assertGreater(programmes["eco"]["dry_tail_minutes"], 0)
        for name, profile in programmes.items():
            if name != "eco":
                self.assertFalse(profile.get("end_blip"), name)

    def test_fallback_defaults_mirror_the_yaml(self):
        orig = dm.DishwasherMonitor.PROGRAMME_PROFILES
        try:
            app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
            app.args = {"programmes_file": "/nonexistent/dishwasher_programmes.yaml"}
            app.log = lambda *a, **kw: None
            app._load_programme_profiles()
            profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES
            self.assertIs(profiles["eco"]["end_blip"], True)
            self.assertFalse(profiles["quick"].get("end_blip"))
        finally:
            dm.DishwasherMonitor.PROGRAMME_PROFILES = orig


# ---------------------------------------------------------------------------------------------
# A real recorded ECO cycle (fixtures/dishwasher_eco_2026_10_11.json), replayed through the real
# code with a timer wheel.
# ---------------------------------------------------------------------------------------------

class TimerWheel:
    """run_in/cancel_timer/timer_running over a virtual clock; advance() fires due callbacks."""

    def __init__(self, app):
        self.app = app
        self.heap = []
        self.live = {}
        self.seq = 0
        app.run_in = self.run_in
        app.cancel_timer = lambda handle: self.live.pop(handle, None)
        app.timer_running = lambda handle: handle in self.live

    def run_in(self, callback, delay, **kwargs):
        self.seq += 1
        handle = f"wheel#{self.seq}"
        self.live[handle] = (callback, kwargs)
        heapq.heappush(self.heap, (self.app.now + timedelta(seconds=delay), self.seq, handle))
        return handle

    def advance(self, to):
        """Fire the callbacks due by `to`; returns the moment one of them took the state out of
        Running, else None."""
        while self.heap and self.heap[0][0] <= to:
            due, _seq, handle = heapq.heappop(self.heap)
            entry = self.live.pop(handle, None)
            if entry is None:
                continue
            self.app.now = due
            entry[0](entry[1])
            if self.app.states[self.app.state_entity] != "Running":
                return due
        self.app.now = to
        return None


def replay_recorded_eco(profiles, feedback_file):
    """Feed the recorded ECO plug readings from minute 200 on to a monitor armed with a learned
    ECO duration and the 0.95 guard fraction. Returns (app, finished_at, start, record)."""
    fx = json.loads(FIXTURE.read_text())
    base = datetime.fromisoformat(fx["power_base"])
    start = datetime(2026, 10, 10, 22, 51, 52, tzinfo=UTC)
    readings = [(base + timedelta(seconds=t), w) for t, w in fx["power"]]
    from_t = start + timedelta(minutes=200)

    dm.DishwasherMonitor.PROGRAMME_PROFILES = {k: dict(v) for k, v in profiles.items()}
    app = make_running_app(from_t, energy_now="1.71", energy_start=1.0)
    app.start_time = start
    app.finish_guard_fraction = 0.95
    app._learned_durations = {"eco": {"n": 53, "avg": 224.3}}
    app._last_high_power_time = max(t for t, w in readings if w >= 50)
    app.feedback_file = feedback_file
    wheel = TimerWheel(app)

    finished_at = None
    for t, w in readings:
        if t < from_t:
            continue
        finished_at = wheel.advance(t)
        if finished_at is not None:
            break
        power(app, w)
    return app, finished_at, start, fx["record"]


class RecordedEcoTrace(unittest.TestCase):
    def setUp(self):
        self._orig_profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig_profiles

    def _run(self, profiles):
        with tempfile.TemporaryDirectory() as tmp:
            feedback = os.path.join(tmp, "dishwasher_feedback.json")
            app, finished_at, start, record = replay_recorded_eco(profiles, feedback)
            with open(feedback) as f:
                cycles = json.load(f)["cycles"]
        return app, finished_at, start, record, cycles

    def test_without_the_rule_the_replay_matches_what_production_recorded(self):
        """The harness is faithful: the tail path lands on the recorded timestamp and duration."""
        app, finished_at, start, record, cycles = self._run(_TEST_PROFILES)
        self.assertIsNotNone(finished_at)
        recorded_ts = datetime.fromisoformat(record["ts"])
        self.assertAlmostEqual((finished_at - recorded_ts).total_seconds(), 0, delta=2)
        self.assertEqual(len(cycles), 1)
        self.assertAlmostEqual(cycles[0]["duration_min"], record["duration_min"], delta=0.2)

    def test_with_the_rule_it_finishes_at_the_blip_and_records_the_same_duration(self):
        app, finished_at, start, record, cycles = self._run(BLIP_PROFILES)
        self.assertIsNotNone(finished_at)
        recorded_ts = datetime.fromisoformat(record["ts"])
        early_min = (recorded_ts - finished_at).total_seconds() / 60
        self.assertGreater(early_min, 17.0)
        self.assertLess(early_min, 19.5)
        self.assertEqual(app.sonos_notifier.calls, [ANNOUNCEMENT])
        self.assertEqual(len(cycles), 1)
        self.assertAlmostEqual(cycles[0]["duration_min"], record["duration_min"], delta=0.2)
        self.assertEqual(cycles[0]["end_reason"], record["end_reason"])
        self.assertEqual(cycles[0]["confirmed"], record["confirmed"])
        self.assertAlmostEqual((finished_at - start).total_seconds() / 60, record["duration_min"], delta=0.2)


if __name__ == "__main__":
    unittest.main()
