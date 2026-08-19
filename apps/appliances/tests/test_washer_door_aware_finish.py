# tests/test_washer_door_aware_finish.py - Phase-1 reliability fixes at the finish transition.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# Covers, driving the REAL _transition_to_unemptied / _transition_to_emptied / _restore_reconcile
# / _unemptied_door_recheck (this repo's incident history says stubbing the very delegate whose
# caller-interaction is the fix hides the bug again - test_washer_restart_survival.py's header):
#
#   FIX 2  door-aware finish: a power/timer/backstop finish where the human already emptied
#          (door open now, or a recorder door-open edge since the finish anchor) routes to
#          Emptied and never announces; feedback still saved exactly once.
#   FIX 3  announce freshness: a finish detected long after it happened pushes the phone instead
#          of blasting Sonos.
#   FIX 1b restore reconcile: an uncorroborated restore that has stayed at standby concludes the
#          cycle quietly (history-corrected duration, no Sonos).
#   FIX 4  Unemptied reconciler: an ajar door (open edge in history, contact reads closed now)
#          moves Unemptied -> Emptied.
#   REGRESSION GUARD: an untouched-door backstop finish still announces Unemptied exactly as today.
#   D1     (2026-08-19 adversarial pass) _finish_anchor()'s fallback chain: on the reconcile
#          path, _finish_anchor_override pins the anchor to start_time + addload_window_minutes
#          for the duration of the transition - never a boot-seeded/distrusted last_high_energy_at
#          - so a door edge that predates the add-load window is excluded, one that postdates it
#          is caught even with sparse power history, and Sonos is structurally impossible (the
#          override always makes freshness latency huge).
#
# Only the AppDaemon surface and the feedback/notify/classify LEAVES are faked; every door /
# freshness / reconcile decision runs for real. Any state_file stays in a tmpdir (none is needed
# here - no _set_state_entity store write path is exercised, _set_state_entity itself is faked).

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

import washer_monitor as wm  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 19, 14, 55, 0, tzinfo=UTC)
START = NOW - timedelta(minutes=168)  # a real ~2.8h wash, like the 2026-08-19 incident


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def door_series(*opens_closes):
    """Build a door history series from (state, minutes_before_now) pairs, e.g.
    ("off", 8), ("on", 3) = closed 8 min ago, opened 3 min ago."""
    return [{"state": s, "last_changed": _iso(NOW - timedelta(minutes=m))} for s, m in opens_closes]


def make_finish_app(
    *,
    state="Running",
    pending_end_reason=None,
    door_now="off",
    door_history=None,
    power_history=None,
    last_high_energy_at=NOW,
    restored_uncorroborated=False,
    now=NOW,
    start=START,
):
    """A Running (or Unemptied) WasherMonitor wired so the real finish/door/reconcile logic runs;
    only leaves (feedback save, classify, programme resolve, notifiers) are faked."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.args = {}
    app.now = now
    app._now_utc = lambda: app.now

    # ---- cycle knobs / state ----
    app.min_cycle_minutes = 25
    app.min_energy_kwh = 0.2
    app.max_running_hours = 5
    app.cooling_period = 300
    app.detect_delayed_start = False
    app._delayed_start_trimmed = False
    app.start_time = start
    app.state = state
    app.last_state_change = None
    app.notification_sent = False
    app.programme_confirmed_by_user = False
    app.confirmed_by_username = None
    app.expected_dur_at_start = None
    app.heating_phase_count = 0
    app.max_power_seen = 40.0
    app.observed_heating = True
    app._guard_bar_class = None
    app._live_class_key = None
    app._live_class_since = None
    app._cycle_actor = None
    app._session_cost_kr = 0.0
    app.track_cycle_cost = False
    app.last_door_closed_at = None
    app.last_door_closed_trusted = False
    app._pending_end_reason = pending_end_reason
    app._pending_tail_mean_w = None
    app._pending_tail_std_w = None
    app._pending_tail_peak_w = None
    app._last_saved_record_ts = None
    app.in_finishing_tail = False
    app.in_finishing_tail_entered_at = None
    app.last_tail_pulse_at = None
    app.tail_pattern_locked = False
    app.tail_pattern_cycle_seconds = None
    app.tail_pattern_last_pulse_at = None
    app.tail_pattern_locked_at = None

    # ---- Phase-1 attributes under test ----
    app.last_high_energy_at = last_high_energy_at
    app.anti_crease_window_minutes = 8
    app.announce_freshness_minutes = 15
    app.restored_uncorroborated = restored_uncorroborated
    app._restore_reconcile_timer = None
    app.restore_corroboration_window_minutes = 10
    app.finish_standby_max_watts = 8.0
    app._unemptied_last_history_check_at = None
    # ---- D1 attributes (2026-08-19 adversarial pass) ----
    app.addload_window_minutes = 5
    app._finish_anchor_override = None
    app._store_state_since = None

    # ---- entities ----
    app.state_entity = "sensor.washer_state"
    app.power_sensor = "sensor.washer_plug_power"
    app.energy_sensor = "sensor.washer_plug_energy"
    app.door_sensor = "binary_sensor.washer_door_contact"
    app.door_sensor_inverted = False
    app.start_w = 18.0
    app.announce_entity = None
    app.announce_message = "Washer is ready to be emptied"
    app.door_lock_entity = None
    app.unemptied_timeout_hours = 24
    app.emptied_timeout_minutes = 30
    app.notify_target = ["mikkel"]

    # ---- timers (all None; harness records run_in) ----
    app.poll_timer = None
    app.history_poll_timer = None
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.unemptied_door_recheck_timer = None
    app.emptied_watchdog_timer = None

    # ---- fake HA surface ----
    app.states = {
        app.state_entity: state,
        app.power_sensor: "0.0",
        app.door_sensor: door_now,
    }
    app._history = {}
    if door_history is not None:
        app._history[app.door_sensor] = door_history
    if power_history is not None:
        app._history[app.power_sensor] = power_history

    def get_state(entity, attribute=None, **kw):
        if attribute == "all":
            return {"state": app.states.get(entity), "attributes": {}, "last_changed": _iso(app.now)}
        return app.states.get(entity)

    app.get_state = get_state
    app.get_history = lambda entity_id=None, **kw: [list(app._history.get(entity_id, []))]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.run_in = lambda cb, delay, **kw: object()
    app._safe_cancel_timer = lambda handle: None
    app.call_service = lambda *a, **kw: None
    # Leaf publish (HA set_state + durable store + UI select): faked wholesale, as the other
    # washer suites do, so no real entity/store I/O happens - the finish/door logic is upstream.
    app.set_state_calls = []
    app._set_state_entity = lambda state=None, attributes=None, **kw: app.set_state_calls.append(
        {"state": state, "attributes": attributes or {}}
    )

    # ---- notifier leaves (recorders; _push_mobile itself runs for real) ----
    app.sonos_calls = []
    app.sonos_notifier = types.SimpleNamespace(notify=lambda message: app.sonos_calls.append(message))
    app.mobile_calls = []
    mobile = types.SimpleNamespace(notify=lambda **kw: app.mobile_calls.append(kw) or "coro")
    app.get_app = lambda name: mobile if name == "MobileNotifier" else None
    app.create_task = lambda coro: None

    # ---- feedback / classify / programme leaves ----
    app.saved_feedback = []
    app._save_cycle_feedback = lambda **kw: (app.saved_feedback.append(kw) or {"ts": "t", **kw})
    app._maybe_send_confirm_push = lambda record: None
    app._schedule_vibration_unload_patch = lambda record: None
    app._patch_cycle_record = lambda ts, patch: app.__dict__.setdefault("patch_calls", []).append((ts, patch))
    app._get_selected_options = lambda: {}
    app._vibration_summary = lambda: None
    app._get_spin_rpm_for_feedback = lambda: None
    app._set_programme_helpers_default = lambda: None
    app._get_energy_used = lambda: 0.85
    app._compute_final_and_confirmed_programme = lambda run, en, update_detected=False: ("eco", None, "eco", None)
    app._get_profile = lambda prog, temp: {"label": "Eco", "heats": False}
    app._attribute = lambda event: {"person": "mikkel", "method": "sole_occupant"}
    app._classify_cycle_completion = lambda **kw: {
        "completion_class": "completed",
        "valid_for_learning": True,
        "validation_flags": [],
        "end_reason": kw.get("transition_path"),
        "programme_key_used_for_validation": "eco",
    }
    app._get_programme_duration_hint_for_history = lambda: None
    app._get_user_cycle_end_time = lambda: None
    app._clear_cycle_ended_entity = lambda: None
    app.cycle_ended_at_entity = None
    return app


def logged(app, needle):
    return any(needle in str(a[0]) for a, _kw in app.log_calls)


class Fix2DoorAwareFinish(unittest.TestCase):
    def test_backstop_finish_with_door_edge_routes_to_emptied_no_announce(self):
        """Test 4: a backstop finish (gate skipped) where the recorder shows a door-open edge
        after the finish anchor -> Emptied, never announced; feedback saved exactly once."""
        app = make_finish_app(
            pending_end_reason="standby_backstop",
            door_now="off",  # contact reads closed now
            door_history=door_series(("off", 200), ("on", 3)),  # opened 3 min ago
            last_high_energy_at=NOW - timedelta(minutes=6),
        )
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(app.mobile_calls, [])
        self.assertEqual(len(app.saved_feedback), 1)
        self.assertTrue(logged(app, "routing to Emptied"))

    def test_physically_open_door_at_finish_routes_to_emptied(self):
        """The door is open right now at a low-power finish -> Emptied, no announce."""
        app = make_finish_app(
            pending_end_reason="standby_backstop",
            door_now="on",  # open right now
            door_history=None,
        )
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.saved_feedback), 1)


class RegressionGuardUntouchedDoor(unittest.TestCase):
    def test_backstop_finish_untouched_door_still_announces_unemptied(self):
        """Test 5 (REGRESSION GUARD): door never opened, no edge in history -> Unemptied +
        Sonos announcement, feedback with end_reason=standby_backstop, exactly as before FIX 2."""
        app = make_finish_app(
            pending_end_reason="standby_backstop",
            door_now="off",
            door_history=[],  # recorder shows nothing
            last_high_energy_at=NOW,  # detected on time -> fresh
        )
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, ["Washer is ready to be emptied"])
        self.assertEqual(app.mobile_calls, [])
        self.assertEqual(len(app.saved_feedback), 1)
        self.assertEqual(app.saved_feedback[0]["end_reason"], "standby_backstop")


class Fix3AnnounceFreshness(unittest.TestCase):
    def test_late_detection_pushes_mobile_not_sonos(self):
        """Test 6: finish detected 20 min after the anchor, no door activity -> mobile push,
        no Sonos; notification_sent still latches to prevent a double-notify."""
        app = make_finish_app(
            pending_end_reason="standby_backstop",
            door_now="off",
            door_history=[],
            last_high_energy_at=NOW - timedelta(minutes=20),
        )
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertIn("20 min ago", app.mobile_calls[0]["message"])
        self.assertTrue(app.notification_sent)


class Fix1bRestoreReconcile(unittest.TestCase):
    def test_quiet_conclude_saves_once_history_corrected_push_not_sonos(self):
        """Test 3: an uncorroborated restored Running whose machine has stayed at standby the
        whole window and whose last high power was 4h ago concludes - one feedback save with a
        history-corrected (shortened) duration, and the FRESHNESS gate downgrades the
        announcement to a mobile push (late but never silent), never Sonos."""
        start = NOW - timedelta(hours=6)
        app = make_finish_app(
            state="Running",
            restored_uncorroborated=True,
            start=start,
            last_high_energy_at=NOW - timedelta(hours=4),
            power_history=[{"state": "0.0", "last_changed": _iso(NOW - timedelta(minutes=m))}
                           for m in (9, 6, 3, 1)],  # flat idle across the corroboration window
        )
        app._restore_reconcile({})
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertIn("min ago", app.mobile_calls[0]["message"])
        self.assertTrue(app.notification_sent)
        self.assertEqual(len(app.saved_feedback), 1)
        rec = app.saved_feedback[0]
        self.assertEqual(rec["duration_source"], "history_corrected")
        # history put the real end ~4h ago, far short of the 6h wall clock
        self.assertLess(rec["duration_min"], 360)
        self.assertTrue(logged(app, "concluding quietly"))

    def test_reconcile_leaves_running_when_power_still_high(self):
        """A power sample above standby inside the window means the machine is NOT off - the
        reconcile must leave it Running (no premature conclude)."""
        start = NOW - timedelta(hours=6)
        app = make_finish_app(
            state="Running",
            restored_uncorroborated=True,
            start=start,
            last_high_energy_at=NOW - timedelta(minutes=2),
            power_history=[{"state": "1500.0", "last_changed": _iso(NOW - timedelta(minutes=2))}],
        )
        app._restore_reconcile({})
        self.assertEqual(app.state, "Running")
        self.assertEqual(app.saved_feedback, [])

    def test_sparse_history_with_door_edge_after_addload_routes_to_emptied(self):
        """Test iii (D1): last_high_energy_at is None (a sparse recorder gap, not a fact) - the
        override anchor (start + addload_window_minutes) still catches a door-open edge 11 min
        ago, long after the add-load window closed -> Emptied, never Sonos, never a push."""
        start = NOW - timedelta(hours=6)
        app = make_finish_app(
            state="Running",
            restored_uncorroborated=True,
            start=start,
            last_high_energy_at=None,
            door_now="off",  # ajar: reads closed now
            door_history=door_series(("off", 200), ("on", 11)),  # opened 11 min ago
            power_history=[{"state": "0.0", "last_changed": _iso(NOW - timedelta(minutes=m))}
                           for m in (9, 6, 3, 1)],
        )
        app._restore_reconcile({})
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(app.mobile_calls, [])
        self.assertEqual(len(app.saved_feedback), 1)

    def test_boot_seeded_recent_last_high_still_forces_push_not_sonos(self):
        """Test iv (D1): last_high_energy_at is recent (a boot-seeded synthetic placeholder, not
        a fact of when the wash actually finished) and there is no door edge at all. Without the
        override this recent last_high would look fresh and fire Sonos; the override pins the
        anchor to start + addload_window_minutes instead, so the freshness gate still downgrades
        to a mobile push - Sonos is structurally impossible on this path."""
        start = NOW - timedelta(hours=6)
        app = make_finish_app(
            state="Running",
            restored_uncorroborated=True,
            start=start,
            last_high_energy_at=NOW - timedelta(minutes=2),  # synthetic/boot-seeded, looks fresh
            door_now="off",
            door_history=[],  # no door activity at all
            power_history=[{"state": "0.0", "last_changed": _iso(NOW - timedelta(minutes=m))}
                           for m in (9, 6, 3, 1)],
        )
        app._restore_reconcile({})
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertTrue(app.notification_sent)
        self.assertEqual(len(app.saved_feedback), 1)

    def test_addload_edge_at_the_override_boundary_is_not_an_emptying(self):
        """Test v (D1): a door-open edge recorded exactly at start + addload_window_minutes (the
        add-load boundary) predates the override anchor's floor (door-history lookback is
        strictly-after, see _door_open_edge_since) and must NOT be treated as an emptying - the
        drum door is only reliably interlocked shut once the add-load window has fully closed.
        Reconcile still concludes (Unemptied), but only ever via the push, never Sonos."""
        start = NOW - timedelta(hours=6)
        addload_edge = start + timedelta(minutes=5)  # == default addload_window_minutes
        app = make_finish_app(
            state="Running",
            restored_uncorroborated=True,
            start=start,
            last_high_energy_at=None,
            door_now="off",
            door_history=[
                {"state": "off", "last_changed": _iso(start - timedelta(minutes=10))},
                {"state": "on", "last_changed": _iso(addload_edge)},
                {"state": "off", "last_changed": _iso(addload_edge + timedelta(minutes=1))},
            ],
            power_history=[{"state": "0.0", "last_changed": _iso(NOW - timedelta(minutes=m))}
                           for m in (9, 6, 3, 1)],
        )
        app.addload_window_minutes = 5
        app._restore_reconcile({})
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertEqual(len(app.saved_feedback), 1)


class Fix4UnemptiedReconciler(unittest.TestCase):
    def test_ajar_door_edge_in_history_moves_unemptied_to_emptied(self):
        """Test 7: while Unemptied the door was opened AND closed again (ajar) - the live contact
        reads closed, but the recorder shows a door-open edge since finish -> Emptied within one
        5-min history check."""
        app = make_finish_app(
            state="Unemptied",
            door_now="off",  # ajar: reads closed now
            door_history=door_series(("off", 60), ("on", 8), ("off", 6)),  # opened then re-closed
            last_high_energy_at=NOW - timedelta(minutes=30),
        )
        app.states[app.power_sensor] = "0.0"
        app._unemptied_door_recheck({})
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertTrue(logged(app, "ajar"))

    def test_recheck_rate_limits_history_call(self):
        """The recorder query is rate-limited: a check that just ran (marker set to now) must not
        query history again on the very next 60s tick, so a closed door with a stale marker
        simply re-arms without concluding."""
        app = make_finish_app(
            state="Unemptied",
            door_now="off",
            door_history=door_series(("off", 60), ("on", 8), ("off", 6)),
            last_high_energy_at=NOW - timedelta(minutes=30),
        )
        app.states[app.power_sensor] = "0.0"
        app._unemptied_last_history_check_at = NOW - timedelta(seconds=90)  # last checked 90s ago
        app._unemptied_door_recheck({})
        self.assertEqual(app.state, "Unemptied")  # within 5 min -> no history query, stays put
        self.assertIsNotNone(app.unemptied_door_recheck_timer)


if __name__ == "__main__":
    unittest.main()
