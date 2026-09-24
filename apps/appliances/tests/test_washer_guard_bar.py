# tests/test_washer_guard_bar.py - the expected-duration bar (ETA) and the standby finish transition.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# 2026-08-11 incident (washer_monitor.log, local times): Running 11:56:56; first classification
# 'eco' at 12:05:08 froze expected_dur_at_start at 199 min; the classifier then re-classified
# 8+ times (bomuld 60 <-> eco <-> finvask 30) through 14:05. The bar is no longer frozen forever
# at the first guess: it raises to any longer live classification, and LOWERS only once the bar's
# own programme is energy-disproven and the shorter live key has held stable
# (wcls.resolve_guard_bar). 'finvask' (65 min) held stably for 64 minutes during a mid-cycle soak,
# which is why plain stability is not enough. The bar drives the ETA only; finish decisions read
# the plug (washer_plug) and carry no programme-duration guards.

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
    """WasherMonitor with production bar knobs and a controllable clock. The guard-bar methods run
    FOR REAL; only transitions, logging and entity access are stubbed."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)

    # Controllable clock
    app.now = start
    app._now_utc = lambda: app.now

    # Production defaults (washer.yaml / initialize())
    app.min_cycle_minutes = 25
    app.min_energy_kwh = 0.2
    app.max_running_hours = 5
    app.guard_reclass_stable_minutes = 15.0
    app.guard_energy_disproof_margin = 1.10

    # Cycle state
    app.state = "Running"
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
    app._pending_end_reason = None

    # Stubbed surface
    app.states = {}
    app.log_calls = []
    app.unemptied_calls = []
    app.off_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)

    # Transitions land here (no cooling-period/gate logic to simulate in this fixture).
    def _stub_transition_to_unemptied(**kw):
        app.unemptied_calls.append(dict(kw))
        app.state = "Unemptied"

    def _stub_transition_to_off(reason, force=False):
        app.off_calls.append((reason, force))
        app.state = "Off"

    app._transition_to_unemptied = _stub_transition_to_unemptied
    app._transition_to_off = _stub_transition_to_off
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
    so the bar correctly never lowers."""

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


class TestGuardFollowsCorrectedClassification(unittest.TestCase):
    """The task's hypothetical variant of 2026-08-11: the machine was really running Bomuld 60
    (nominal 149 min; the learned ~156 is deliberately NOT used for guards - use_learned=False,
    polluted learning must never shorten a guard). Once cumulative energy disproves the frozen
    eco (>{0.78*1.1:.3} kWh) and bomuld60 holds stable 15 min, the bar follows the live
    classification."""

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


class TestAntiFlap(unittest.TestCase):
    """Classification bouncing between keys must never oscillate the bar."""

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
    """A HUMAN-confirmed programme outranks the live classification."""

    def confirmed_app(self, label, temp_label):
        app = make_app()
        app.programme_confirmed_by_user = True
        app.states[app.confirm_entity] = label
        app.states[app.temperature_entity] = temp_label
        return app

    def test_confirmed_classification_pins_live_key(self):
        """_classify_programme returns the user's key while confirmed, so the guard-bar
        update converges to the human's choice and never fights it."""
        app = self.confirmed_app("Bomuld", "60°C")
        app.now = app.start_time + timedelta(minutes=60)
        app.energy_used = 0.9
        self.assertEqual(app._classify_programme(), ("bomuld", "60°C"))


class TestUnemptiedTransitionForReal(unittest.TestCase):
    """Drive the REAL _transition_to_unemptied for a standby finish (this repo's incident history
    says stubbing the delegate hides caller/delegate bugs): the standby end reason carries its own
    evidence (3 min of consecutive plug reads <= 3 W after wash activity), so it skips the
    recorder power-pattern gate, announces over Sonos, and saves one record with end_reason=standby."""

    def full_app(self):
        app = make_app()
        tick(app, 8.2, "eco", None, 0.55)
        app.now = app.start_time + timedelta(minutes=179.97)
        # A standby finish on time: the last high sample was minutes ago.
        app.last_high_energy_at = app.now - timedelta(minutes=5)
        app._spin_end_at = None

        # ---- _transition_to_unemptied surface ----
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
        # Power gate that would REFUSE - proves the standby reason skips it.
        app.gate_queries = []
        app._power_looks_like_cycle_end = lambda *a, **kw: (app.gate_queries.append(1) or (False, 200.0, 500.0))

        app.notifications = []
        app.sonos_notifier = types.SimpleNamespace(notify=lambda message: app.notifications.append(message))
        # Restore the real transition (make_app stubbed it with a recorder).
        app._transition_to_unemptied = lambda **kw: wm.WasherMonitor._transition_to_unemptied(app, **kw)
        return app

    def test_standby_end_reason_skips_gate_announces_and_saves_feedback(self):
        app = self.full_app()
        app._pending_end_reason = "standby"
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.gate_queries, [])
        self.assertEqual(app.notifications, ["Washer is ready to be emptied"])
        self.assertEqual(len(app.saved_feedback), 1)
        self.assertEqual(app.saved_feedback[0]["end_reason"], "standby")
        unemptied = [c for c in app.set_state_calls if c["state"] == "Unemptied"]
        self.assertEqual(len(unemptied), 1)
        self.assertEqual(unemptied[0]["attributes"]["end_reason"], "standby")
        self.assertTrue(unemptied[0]["attributes"]["cycle_complete"])

    def test_plain_low_power_path_still_honours_the_gate(self):
        app = self.full_app()
        app._pending_end_reason = None
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Running")       # blocked by the refusing gate
        self.assertEqual(app.gate_queries, [1])
        self.assertEqual(app.notifications, [])
        self.assertEqual(app.saved_feedback, [])

    def test_end_reasons_are_known_transition_paths(self):
        # Feedback migration must not normalize these away - old records carry the retired ones.
        for reason in ("standby", "spin_end", "standby_backstop", "tail_to_standby", "anti_crease_pattern"):
            self.assertIn(reason, wcls.KNOWN_TRANSITION_PATHS)


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
