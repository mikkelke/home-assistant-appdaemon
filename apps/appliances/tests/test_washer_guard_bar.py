# tests/test_washer_guard_bar.py - the finish-guard duration bar and the standby-backstop net.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# 2026-08-11 incident (washer_monitor.log, local times): Running 11:56:56; first classification
# 'eco' at 12:05:08 froze expected_dur_at_start at 199 min; the classifier then re-classified
# 8+ times (bomuld 60 <-> eco <-> finvask 30) through 14:05; the machine stopped drawing power
# ~14:06; the finish guards kept demanding 92% of 199 = 183 min right up to the standby
# backstop, which at 14:56:54 ("run 179 min") forced a silent Off: no announcement, no
# Unemptied, no learning record. 12 such endings retained since 2026-03-04 (~11% of real washes).
#
# Two independent fixes under test, driving the REAL WasherMonitor methods (stubbed AppDaemon
# surface, same harness style as test_washer_unavailable_grace.py):
#
#  1. The guard bar (expected_dur_at_start) is no longer frozen forever at the first guess:
#     it raises to any longer live classification, and LOWERS only once the bar's own
#     programme is energy-disproven and the shorter live key has held stable
#     (wcls.resolve_guard_bar). The same tape shows why plain "stable for N minutes" is NOT
#     enough: 'finvask' (65 min) held stably for 64 minutes (13:01-14:05) during a mid-cycle
#     soak - lowering on stability alone would have false-announced a drum full of water at
#     run ~100 min. Energy is monotone and soak-proof; rate/centroid matches are not.
#
#  2. The standby-backstop net: 5+ min of hard 0W on a cycle that demonstrably heated
#     (observed_heating) and consumed >= min_energy_kwh now publishes Unemptied (announce +
#     feedback) instead of silently forcing Off. Existing protections stay: the
#     finish_min_run_minutes_warm floor (100 min) and the 5-minute zero-power requirement.

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

import washer_classify as wcls  # noqa: E402
import washer_monitor as wm  # noqa: E402

# 2026-08-11 11:56:56 local Europe/Copenhagen (+02) = 09:56:56 UTC.
CYCLE_START = datetime(2026, 8, 11, 9, 56, 56, tzinfo=timezone.utc)


def make_app(start=CYCLE_START):
    """WasherMonitor with production guard knobs and a controllable clock. All guard-bar,
    guard-duration and standby-backstop methods run FOR REAL; only transitions, logging and
    entity access are stubbed."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)

    # Controllable clock
    app.now = start
    app._now_utc = lambda: app.now

    # Production defaults (washer.yaml / initialize())
    app.finish_guard_fraction = 0.92
    app.finish_min_run_minutes_warm = 100.0
    app.finish_min_run_minutes_cold = 50.0
    app.min_cycle_minutes = 25
    app.min_energy_kwh = 0.2
    app.max_running_hours = 5
    app.guard_reclass_stable_minutes = 15.0
    app.guard_energy_disproof_margin = 1.10

    # Cycle state
    app.start_time = start
    app.energy_used = 0.0
    app._get_energy_used = lambda: app.energy_used
    app.observed_heating = True
    app.heating_phase_count = 2
    app.max_power_seen = 2114.0
    app.programme_confirmed_by_user = False
    app.confirm_entity = "input_select.washer_confirmed_programme"
    app.temperature_entity = "input_select.washer_temperature"
    app.expected_dur_at_start = None
    app._guard_bar_class = None
    app._live_class_key = None
    app._live_class_since = None
    app._learned_durations = {}
    app._zero_power_since = None
    app._pending_end_reason = None

    # Stubbed surface
    app.states = {}
    app.log_calls = []
    app.unemptied_calls = []
    app.off_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app._transition_to_unemptied = lambda **kw: app.unemptied_calls.append(dict(kw))
    app._transition_to_off = lambda reason, force=False: app.off_calls.append((reason, force))
    return app


def tick(app, minutes_from_start, prog, temp, energy_kwh):
    """One _check_energy_finish classification tick: advance the clock, then run the real
    stability tracking + guard-bar update in the same order as the production tick."""
    app.now = app.start_time + timedelta(minutes=minutes_from_start)
    app.energy_used = energy_kwh
    app._note_live_classification(prog, temp)
    app._update_guard_bar(prog, temp)


def logged(app, needle):
    return any(needle in str(a[0]) for a, _ in app.log_calls)


class TestResolveGuardBar(unittest.TestCase):
    """Pure decision function (washer_classify.resolve_guard_bar)."""

    def test_freezes_first_classification(self):
        self.assertEqual(wcls.resolve_guard_bar(None, None, 199, 0.78, 0.0, 0.1, 15), (199, "freeze"))

    def test_unknown_live_keeps_bar(self):
        self.assertEqual(wcls.resolve_guard_bar(199, 0.78, None, None, 0.0, 0.5, 15), (199, None))

    def test_raises_immediately_without_stability(self):
        # Ekspres (20) misfrozen under an actual warm programme: first eco tick raises the bar.
        self.assertEqual(wcls.resolve_guard_bar(20, 0.40, 199, 0.78, 0.0, 0.3, 15), (199, "raise"))

    def test_equal_duration_keeps_bar(self):
        self.assertEqual(wcls.resolve_guard_bar(199, 0.78, 199, 0.78, 60.0, 0.5, 15), (199, None))

    def test_equal_duration_rekeys_when_bar_programme_unknown(self):
        # Restart restored the bar as a bare float - adopt the live key so disproof can work.
        self.assertEqual(wcls.resolve_guard_bar(199, None, 199, 0.78, 0.0, 0.5, 15), (199, "rekey"))

    def test_no_lower_without_energy_disproof(self):
        # finvask stable for 64 min during the 2026-08-11 soak, but 0.50 kWh does not disprove
        # eco (max 0.78 * 1.1 = 0.858) - the bar must hold. THE protection the freeze existed for.
        self.assertEqual(wcls.resolve_guard_bar(199, 0.78, 65, 0.38, 64.0, 0.50, 15), (199, None))

    def test_no_lower_when_unstable(self):
        # Energy disproves eco but the live key just changed (boundary flapping) - hold.
        self.assertEqual(wcls.resolve_guard_bar(199, 0.78, 149, 1.40, 5.0, 1.02, 15), (199, None))

    def test_no_lower_when_bar_programme_unknown(self):
        self.assertEqual(wcls.resolve_guard_bar(199, None, 149, 1.40, 30.0, 1.02, 15), (199, None))

    def test_no_lower_when_live_cannot_explain_energy(self):
        # 1.02 kWh also exceeds a hypothetical live key's own ceiling - misclassification.
        self.assertEqual(wcls.resolve_guard_bar(199, 0.78, 65, 0.38, 30.0, 1.02, 15), (199, None))

    def test_lowers_with_disproof_stability_and_consistency(self):
        self.assertEqual(wcls.resolve_guard_bar(199, 0.78, 149, 1.40, 15.0, 1.02, 15), (149, "lower"))


class TestIncidentTapeRegression(unittest.TestCase):
    """Replay of the actual 2026-08-11 log tape. Final energy stayed eco-plausible (~0.55 kWh),
    so the guard bar correctly never lowers and the finish guards stay shut - exactly as in
    production. The fix under test is the backstop NET: the 0W-forced ending must now publish
    Unemptied instead of the silent Off at 14:56:54."""

    def replay(self):
        app = make_app()
        # 12:05:08 first classification 'eco' -> frozen 199
        tick(app, 8.2, "eco", None, 0.30)
        self.assertEqual(app.expected_dur_at_start, 199)
        # The logged flip-flops: bomuld60 12:26, eco 12:31, bomuld60 12:31:49, eco 12:32,
        # bomuld60 12:34, finvask30 13:01, eco 14:05. Energy stays <= 0.55 (eco-plausible).
        tick(app, 29.2, "bomuld", "60°C", 0.45)
        tick(app, 34.4, "eco", None, 0.46)
        tick(app, 34.9, "bomuld", "60°C", 0.46)
        tick(app, 35.4, "eco", None, 0.46)
        tick(app, 38.0, "bomuld", "60°C", 0.47)
        tick(app, 64.1, "finvask", "30°C", 0.50)
        tick(app, 128.3, "eco", None, 0.55)
        return app

    def test_bar_never_lowers_on_the_real_tape(self):
        app = self.replay()
        self.assertEqual(app.expected_dur_at_start, 199)
        self.assertFalse(logged(app, "Lowered expected_dur_at_start"))

    def test_finvask_soak_cannot_false_announce(self):
        """finvask|30 was stable 13:01->14:05 (64 min) during a power-quiet soak. At run 100-128
        the guards must STILL be shut (a stability-only rule would have opened them at ~100)."""
        app = self.replay()
        # Mid-soak checkpoints: run 100 and 128 min, live key finvask stable > 15 min.
        for run_min in (100.0, 128.0):
            app.now = app.start_time + timedelta(minutes=run_min)
            guard = app._get_guard_duration("finvask", "30°C", ("finvask", "30°C"))
            self.assertEqual(guard, 199)
            self.assertFalse(app._meets_finish_time_guards(run_min, guard))

    def test_forced_off_becomes_unemptied_via_net(self):
        """14:56:54: run 179.97 min, 0W since 14:51:48 (5.1 min), guards still shut (183 min bar).
        Old behaviour: silent forced Off. New: Unemptied with end_reason standby_backstop."""
        app = self.replay()
        app.now = app.start_time + timedelta(minutes=179.97)
        app._zero_power_since = app.now - timedelta(minutes=5.1)
        handled = app._standby_backstop_tick(app.now, "eco", None, ("eco", None))
        self.assertTrue(handled)
        self.assertEqual(len(app.unemptied_calls), 1)
        self.assertEqual(app.off_calls, [])
        self.assertEqual(app._pending_end_reason, "standby_backstop")
        self.assertTrue(logged(app, "Standby backstop net"))

    def test_net_does_not_preempt_the_normal_finish(self):
        """When the guards ARE met the ordinary 3-minute standby finish still wins (no
        standby_backstop end reason)."""
        app = self.replay()
        # Hypothetical: guards satisfied (run past 92% of 199).
        app.now = app.start_time + timedelta(minutes=190.0)
        app._zero_power_since = app.now - timedelta(minutes=3.2)
        handled = app._standby_backstop_tick(app.now, "eco", None, ("eco", None))
        self.assertTrue(handled)
        self.assertEqual(len(app.unemptied_calls), 1)
        self.assertIsNone(app._pending_end_reason)


class TestGuardFollowsCorrectedClassification(unittest.TestCase):
    """The task's hypothetical variant of 2026-08-11: the machine was really running Bomuld 60
    (nominal 149 min; the learned ~156 is deliberately NOT used for guards - use_learned=False,
    polluted learning must never shorten a guard). Once cumulative energy disproves the frozen
    eco (>{0.78*1.1:.3} kWh) and bomuld60 holds stable 15 min, the bar follows the live
    classification and the wash announces on time through the NORMAL standby path."""

    def build(self):
        app = make_app()
        tick(app, 8.2, "eco", None, 0.30)          # frozen eco 199
        self.assertEqual(app.expected_dur_at_start, 199)
        # Energy climbs decisively past the eco ceiling; classifier pins bomuld 60.
        tick(app, 29.2, "bomuld", "60°C", 0.90)    # stability streak starts; 0.90 > 0.858 disproof
        tick(app, 35.0, "bomuld", "60°C", 0.95)    # stable 5.8 min - still held
        self.assertEqual(app.expected_dur_at_start, 199)
        tick(app, 44.2, "bomuld", "60°C", 1.02)    # stable 15.0 min - bar lowers
        return app

    def test_bar_lowers_to_live_programme(self):
        app = self.build()
        self.assertEqual(app.expected_dur_at_start, 149)
        self.assertEqual(app._guard_bar_class, ("bomuld", "60°C"))
        self.assertTrue(logged(app, "Lowered expected_dur_at_start"))

    def test_run_179_now_reaches_unemptied_and_announces(self):
        """Run 179.97 min >= 92% of 149 (137.1) and >= 100 warm floor -> the plain 3-minute
        standby backstop transitions to Unemptied (announce path), no net needed."""
        app = self.build()
        app.now = app.start_time + timedelta(minutes=179.97)
        guard = app._get_guard_duration("bomuld", "60°C", ("bomuld", "60°C"))
        self.assertEqual(guard, 149)
        self.assertTrue(app._meets_finish_time_guards(179.97, guard))
        app._zero_power_since = app.now - timedelta(minutes=3.1)
        handled = app._standby_backstop_tick(app.now, "bomuld", "60°C", ("bomuld", "60°C"))
        self.assertTrue(handled)
        self.assertEqual(len(app.unemptied_calls), 1)
        self.assertEqual(app.off_calls, [])
        self.assertIsNone(app._pending_end_reason)  # normal finish, not the net

    def test_lowered_bar_still_blocks_too_early_finish(self):
        """The lowered 149 bar keeps blocking before 137 min - following the classification
        must not mean announcing mid-cycle."""
        app = self.build()
        app.now = app.start_time + timedelta(minutes=120.0)
        guard = app._get_guard_duration("bomuld", "60°C", ("bomuld", "60°C"))
        self.assertFalse(app._meets_finish_time_guards(120.0, guard))


class TestAntiFlap(unittest.TestCase):
    """Classification bouncing between keys must never oscillate the finish decision."""

    def test_boundary_flapping_never_lowers_the_bar(self):
        """eco<->bomuld60 flipping every 30 s (energy jitter at the 0.85 kWh gate, as in the
        tape 12:26-12:35) - even with energy past the disproof line, no key is ever stable
        long enough to lower the bar."""
        app = make_app()
        tick(app, 10.0, "eco", None, 0.40)
        self.assertEqual(app.expected_dur_at_start, 199)
        minute = 20.0
        for i in range(80):  # 40 minutes of flapping, alternating each 30 s tick
            prog, temp = (("bomuld", "60°C") if i % 2 == 0 else ("eco", None))
            tick(app, minute, prog, temp, 0.90)
            minute += 0.5
        self.assertEqual(app.expected_dur_at_start, 199)
        self.assertFalse(logged(app, "Lowered expected_dur_at_start"))
        # And mid-cycle the guards stay shut against either key's bar.
        run_min = minute
        self.assertFalse(app._meets_finish_time_guards(run_min, app._get_guard_duration("bomuld", "60°C")))

    def test_stability_streak_resets_on_change(self):
        app = make_app()
        tick(app, 10.0, "eco", None, 0.40)
        tick(app, 20.0, "bomuld", "60°C", 0.90)
        self.assertAlmostEqual(app._live_class_stable_minutes("bomuld", "60°C"), 0.0)
        tick(app, 30.0, "bomuld", "60°C", 0.95)
        self.assertAlmostEqual(app._live_class_stable_minutes("bomuld", "60°C"), 10.0)
        tick(app, 31.0, "eco", None, 0.95)           # flap
        tick(app, 32.0, "bomuld", "60°C", 0.96)      # streak restarts
        self.assertAlmostEqual(app._live_class_stable_minutes("bomuld", "60°C"), 0.0)

    def test_raise_is_sticky_through_later_flaps(self):
        """A raised bar does not drop back on the next flap tick - lowering always requires
        the disproof rule, so the bar cannot oscillate with the classifier."""
        app = make_app()
        tick(app, 10.0, "bomuld", "60°C", 0.40)      # frozen 149
        self.assertEqual(app.expected_dur_at_start, 149)
        tick(app, 20.0, "eco", None, 0.45)           # raise 149 -> 199
        self.assertEqual(app.expected_dur_at_start, 199)
        self.assertTrue(logged(app, "Raised expected_dur_at_start"))
        tick(app, 20.5, "bomuld", "60°C", 0.45)      # flap back: 0.45 kWh does not disprove eco
        self.assertEqual(app.expected_dur_at_start, 199)


class TestUserConfirmationSupremacy(unittest.TestCase):
    """A HUMAN-confirmed programme outranks the bar and the live classification in both
    directions, exactly as before."""

    def confirmed_app(self, label, temp_label):
        app = make_app()
        app.programme_confirmed_by_user = True
        app.states[app.confirm_entity] = label
        app.states[app.temperature_entity] = temp_label
        return app

    def test_user_programme_beats_higher_bar(self):
        app = self.confirmed_app("Bomuld", "60°C")
        app.expected_dur_at_start = 199  # stale frozen eco
        app._guard_bar_class = ("eco", None)
        self.assertEqual(app._get_guard_duration("eco", None, ("eco", None)), 149)

    def test_user_programme_beats_lowered_bar_upward(self):
        app = self.confirmed_app("ECO", "40-60°C")
        app.expected_dur_at_start = 149
        app._guard_bar_class = ("bomuld", "60°C")
        self.assertEqual(app._get_guard_duration("bomuld", "60°C", ("bomuld", "60°C")), 199)

    def test_unconfirmed_selector_is_ignored(self):
        """Selector holding a value WITHOUT programme_confirmed_by_user (auto-filled
        prediction) must not drive the guard - falls through to the bar."""
        app = make_app()
        app.states[app.confirm_entity] = "Ekspres"
        app.expected_dur_at_start = 199
        app._guard_bar_class = ("eco", None)
        self.assertEqual(app._get_guard_duration("eco", None, ("eco", None)), 199)

    def test_confirmed_classification_pins_live_key(self):
        """_classify_programme returns the user's key while confirmed, so the guard-bar
        update converges to the human's choice and never fights it."""
        app = self.confirmed_app("Bomuld", "60°C")
        app.now = app.start_time + timedelta(minutes=60)
        app.energy_used = 0.9
        self.assertEqual(app._classify_programme(), ("bomuld", "60°C"))


class TestBackstopNetGuards(unittest.TestCase):
    """The net's own preconditions: everything outside them keeps the existing forced-Off /
    keep-checking behaviour."""

    def stuck_app(self, run_min=179.97, zero_min=5.1, energy=0.55, heated=True):
        app = make_app()
        tick(app, 8.2, "eco", None, 0.30)  # bar 199 -> guards shut at run 179.97
        app.observed_heating = heated
        app.energy_used = energy
        app.now = app.start_time + timedelta(minutes=run_min)
        app._zero_power_since = app.now - timedelta(minutes=zero_min)
        return app

    def test_no_heating_still_forces_off(self):
        app = self.stuck_app(heated=False, run_min=179.97)
        handled = app._standby_backstop_tick(app.now, "eco", None, ("eco", None))
        self.assertTrue(handled)
        self.assertEqual(app.unemptied_calls, [])
        self.assertEqual(len(app.off_calls), 1)

    def test_energy_below_min_still_forces_off(self):
        app = self.stuck_app(energy=0.15)
        handled = app._standby_backstop_tick(app.now, "eco", None, ("eco", None))
        self.assertTrue(handled)
        self.assertEqual(app.unemptied_calls, [])
        self.assertEqual(len(app.off_calls), 1)

    def test_run_below_warm_floor_still_forces_off(self):
        # 100-min warm floor untouched: a heated cycle that 0W-stalls at 95 min stays Off.
        app = self.stuck_app(run_min=95.0)
        handled = app._standby_backstop_tick(app.now, "eco", None, ("eco", None))
        self.assertTrue(handled)
        self.assertEqual(app.unemptied_calls, [])
        self.assertEqual(len(app.off_calls), 1)

    def test_five_minute_zero_power_requirement_untouched(self):
        # 4 min of 0W with guards shut: keep checking, no transition either way.
        app = self.stuck_app(zero_min=4.0)
        handled = app._standby_backstop_tick(app.now, "eco", None, ("eco", None))
        self.assertFalse(handled)
        self.assertEqual(app.unemptied_calls, [])
        self.assertEqual(app.off_calls, [])

    def test_under_three_minutes_does_nothing(self):
        app = self.stuck_app(zero_min=2.0)
        handled = app._standby_backstop_tick(app.now, "eco", None, ("eco", None))
        self.assertFalse(handled)
        self.assertFalse(logged(app, "Standby backstop"))  # not even backstop logging yet


class TestUnemptiedTransitionForReal(unittest.TestCase):
    """Drive the REAL _transition_to_unemptied for the net path (this repo's incident history
    says stubbing the delegate hides caller/delegate bugs): the standby_backstop end reason
    must skip the power-pattern gate (5 min of hard 0W is stronger evidence than the gate),
    announce over Sonos, and save a feedback record carrying end_reason=standby_backstop."""

    def full_app(self):
        app = make_app()
        tick(app, 8.2, "eco", None, 0.55)
        app.now = app.start_time + timedelta(minutes=179.97)

        # ---- _transition_to_unemptied surface ----
        app.in_finishing_tail = False
        app.in_finishing_tail_entered_at = None
        app.last_tail_pulse_at = None
        app.tail_pattern_locked = False
        app.tail_pattern_cycle_seconds = None
        app.tail_pattern_last_pulse_at = None
        app.tail_pattern_locked_at = None
        app.state = "Running"
        app.confirmed_by_username = None
        app.last_state_change = None
        app.cooling_period = 180
        app.detect_delayed_start = False
        app._delayed_start_trimmed = False
        app.states["sensor.washer_state"] = "Running"
        app.state_entity = "sensor.washer_state"
        app.door_lock_entity = None
        app.announce_entity = None
        app.announce_message = "Washer is ready to be emptied"
        app.notification_sent = False
        app.track_cycle_cost = False
        app._session_cost_kr = 0.0
        app._cycle_actor = None
        app.last_door_closed_at = None
        app.last_door_closed_trusted = False
        app._pending_tail_mean_w = None
        app._pending_tail_std_w = None
        app._pending_tail_peak_w = None
        app._last_saved_record_ts = None
        app.completion_guard_fraction = 0.65
        app.completion_guard_fraction_user_confirmed = 0.60
        app.finish_power_gate_max_mean_w = 45.0
        app.finish_power_gate_max_peak_w = 120.0
        app.finish_power_gate_off_max_mean_w = 12.0
        app.finish_power_gate_off_max_peak_w = 60.0
        app.unemptied_timeout_hours = 24
        app.poll_timer = None
        app.history_poll_timer = None
        app.running_watchdog_timer = None
        app.unemptied_watchdog_timer = None
        app.unemptied_door_recheck_timer = None

        # Recorders / harmless stubs
        app.set_state_calls = []
        app._set_state_entity = lambda state=None, attributes=None, **kw: app.set_state_calls.append(
            {"state": state, "attributes": attributes or {}}
        )
        app.saved_feedback = []
        app._save_cycle_feedback = lambda **kw: (app.saved_feedback.append(kw) or {"ts": "t", **kw})
        app._maybe_send_confirm_push = lambda record: None
        app._schedule_vibration_unload_patch = lambda record: None
        app._get_selected_options = lambda: {}
        app._vibration_summary = lambda: None
        app._get_spin_rpm_for_feedback = lambda: None
        app._set_programme_helpers_default = lambda: None
        app._safe_cancel_timer = lambda handle: None
        app.run_in = lambda cb, delay, **kw: object()
        app._correct_duration = lambda wall: (129.2, "power_history")
        app._compute_final_and_confirmed_programme = lambda run, en, update_detected=False: (
            "eco", None, "eco", None
        )
        # Power gate that would REFUSE - proves the standby_backstop reason skips it.
        app.gate_queries = []
        app._power_looks_like_cycle_end = lambda *a, **kw: (app.gate_queries.append(1) or (False, 200.0, 500.0))

        app.notifications = []
        app.sonos_notifier = types.SimpleNamespace(notify=lambda message: app.notifications.append(message))
        # Restore the real transition (make_app stubbed it with a recorder).
        app._transition_to_unemptied = lambda **kw: wm.WasherMonitor._transition_to_unemptied(app, **kw)
        return app

    def test_net_end_reason_skips_gate_announces_and_saves_feedback(self):
        app = self.full_app()
        app._pending_end_reason = "standby_backstop"
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.gate_queries, [])  # gate skipped - 0W x 5 min already verified
        self.assertEqual(app.notifications, ["Washer is ready to be emptied"])
        self.assertEqual(len(app.saved_feedback), 1)
        self.assertEqual(app.saved_feedback[0]["end_reason"], "standby_backstop")
        unemptied = [c for c in app.set_state_calls if c["state"] == "Unemptied"]
        self.assertEqual(len(unemptied), 1)
        self.assertEqual(unemptied[0]["attributes"]["end_reason"], "standby_backstop")
        self.assertTrue(unemptied[0]["attributes"]["cycle_complete"])

    def test_plain_low_power_path_still_honours_the_gate(self):
        app = self.full_app()
        app._pending_end_reason = None
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Running")       # blocked by the refusing gate
        self.assertEqual(app.gate_queries, [1])
        self.assertEqual(app.notifications, [])
        self.assertEqual(app.saved_feedback, [])

    def test_standby_backstop_is_a_known_transition_path(self):
        # Feedback migration must not normalize the new end reason away.
        self.assertIn("standby_backstop", wcls.KNOWN_TRANSITION_PATHS)


class TestGuardBarKeyPersistence(unittest.TestCase):
    """expected_dur_key roundtrip - keeps energy-disproof lowering working across the
    frequent mid-cycle app restarts (deploys)."""

    def test_roundtrip_with_temperature(self):
        app = make_app()
        app._guard_bar_class = ("bomuld", "60°C")
        self.assertEqual(app._guard_bar_key_str(), "bomuld|60°C")
        self.assertEqual(wm.WasherMonitor._parse_guard_bar_key("bomuld|60°C"), ("bomuld", "60°C"))

    def test_roundtrip_without_temperature(self):
        app = make_app()
        app._guard_bar_class = ("eco", None)
        self.assertEqual(app._guard_bar_key_str(), "eco|")
        self.assertEqual(wm.WasherMonitor._parse_guard_bar_key("eco|"), ("eco", None))

    def test_parse_rejects_garbage(self):
        for bad in ("", None, "unknown", "unavailable", "|60°C", 42):
            self.assertIsNone(wm.WasherMonitor._parse_guard_bar_key(bad))

    def test_empty_when_no_class(self):
        app = make_app()
        self.assertEqual(app._guard_bar_key_str(), "")


if __name__ == "__main__":
    unittest.main()
