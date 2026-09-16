# tests/test_washer_dwell_finish_anchor.py - the anti-crease tail observation as a floor for
# finish-detection latency and history-corrected duration on long-dwell programmes.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# last_high_energy_at is stamped only on samples above energy_active_watts (100W) - it is an
# idle-detection timer. An Eco programme legitimately dwells for hours under that bar, so at a
# finish detected on time via the anti-crease pattern that timestamp still sits hours back, and
# two consumers misread it as "when the wash ended":
#   _finish_detection_latency_minutes -> Sonos downgraded to a false "finished N min ago" push
#   _correct_duration (history branch) -> duration / idle_min / the learned record hours short
# _anti_crease_tail_since (stamped on the anti-crease tail_ok tick in _check_energy_finish,
# latched per cycle, persisted in the durable store) floors both. _finish_anchor() itself is
# untouched: its early anchor is the safe side for the door-edge lookback
# (_finish_route_to_emptied), and the reconcile override (D1/D2) must keep winning.
#
# Reuses make_finish_app (test_washer_door_aware_finish.py) for the finish transition and
# make_full_init_app (test_washer_restart_survival.py) for the real initialize()/store
# round-trip, the real _check_energy_finish stamp site and the cycle-boundary clears - only the
# AppDaemon surface is faked, per both suites' own rules.

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_washer_door_aware_finish as dwf  # noqa: E402  (installs the appdaemon stub)
import test_washer_restart_survival as trs  # noqa: E402

NOW = dwf.NOW
_iso = dwf._iso
logged = dwf.logged

# The Eco incident, relative to the tick that finally transitioned.
START = NOW - timedelta(minutes=199.5)
LAST_HIGH = NOW - timedelta(minutes=172)   # last >100W sample: the dwell phase began here
TAIL_SEEN = NOW - timedelta(minutes=16.4)  # anti-crease pattern first confirmed
TAIL_AT_183 = START + timedelta(minutes=183)
FRESHNESS = 20                             # washer.yaml announce_freshness_minutes
LATE_PUSH = "Washer finished about 172 min ago (late detection) - ready to empty."

# Anti-crease tail shape: >= 5 points inside anti_crease_window_minutes, mean <= 45W, peak
# <= 120W - what _power_looks_like_cycle_end needs for the end reasons that don't skip it.
TAIL_SHAPE = [3.0, 40.0, 3.0, 38.0, 3.0, 41.0, 3.0, 2.0]

# Cumulative kWh: the only >100W interval (1.33 kW implied over 0-27 min) ends at +27; the
# history estimate lands on +27.5 (half-way to the next point). Everything after is dwell.
ECO_ENERGY = [(0, 0.000), (27, 0.600), (28, 0.601), (60, 0.605), (120, 0.610), (183, 0.615), (199, 0.616)]


def power_series(now, values, spacing_s=60):
    n = len(values)
    return [
        {"state": str(w), "last_changed": _iso(now - timedelta(seconds=spacing_s * (n - i)))}
        for i, w in enumerate(values)
    ]


def energy_series(start, points):
    return [
        {"state": f"{kwh:.3f}", "last_changed": _iso(start + timedelta(minutes=m))}
        for m, kwh in points
    ]


def make_app(**kw):
    """make_finish_app with the incident's clock and the production freshness knob."""
    kw.setdefault("start", START)
    kw.setdefault("last_high_energy_at", LAST_HIGH)
    kw.setdefault("door_now", "off")
    kw.setdefault("door_history", [])
    kw.setdefault("power_history", power_series(kw.get("now", NOW), TAIL_SHAPE))
    app = dwf.make_finish_app(**kw)
    app.announce_freshness_minutes = FRESHNESS
    return app


class LatencyFloor(unittest.TestCase):
    def test_1_incident_replay_live_tail_stamp_announces_on_sonos(self):
        """Eco finish via anti_crease_pattern, tail confirmed 16.4 min ago, start observed live:
        last_high_energy_at is 172 min stale but the detection is fresh -> Sonos, no push."""
        app = make_app(
            pending_end_reason="anti_crease_pattern",
            anti_crease_tail_since=TAIL_SEEN,
            start_time_source="live",
        )
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, ["Washer is ready to be emptied"])
        self.assertEqual(app.mobile_calls, [])
        self.assertTrue(app.notification_sent)
        self.assertFalse(logged(app, "mobile push instead of Sonos"))
        self.assertEqual(app.saved_feedback[0]["end_reason"], "anti_crease_pattern")
        # The finish transition reads the latch; it must not clear it on the way in.
        self.assertEqual(app._anti_crease_tail_since, TAIL_SEEN)

    def test_2_untrusted_start_with_post_boot_stamp_still_pushes(self):
        """Same finish, but start_time came from the durable store and the stamp was taken
        after this boot -> not evidence; the stale last_high_energy_at still governs."""
        app = make_app(
            pending_end_reason="anti_crease_pattern",
            anti_crease_tail_since=TAIL_SEEN,
            start_time_source="durable_store",
            app_started_at=NOW - timedelta(minutes=30),
        )
        self.assertIsNone(app._trusted_anti_crease_tail_since())
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertEqual(app.mobile_calls[0]["message"], LATE_PUSH)
        self.assertTrue(app.notification_sent)

    def test_3_untrusted_start_with_pre_boot_stamp_is_trusted(self):
        """Start from the durable store, but the stamp predates this boot (restored from the
        store, observed before the restart) -> trusted -> Sonos."""
        app = make_app(
            pending_end_reason="anti_crease_pattern",
            anti_crease_tail_since=TAIL_SEEN,
            start_time_source="durable_store",
            app_started_at=NOW - timedelta(minutes=10),
        )
        self.assertEqual(app._trusted_anti_crease_tail_since(), TAIL_SEEN)
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, ["Washer is ready to be emptied"])
        self.assertEqual(app.mobile_calls, [])

    def test_4_low_power_finish_without_stamp_is_unchanged(self):
        """REGRESSION GUARD: a low_power_detected finish with the same stale last_high_energy_at
        and no anti-crease stamp - latency, message text and the last_high_energy_at+2min
        duration fallback are exactly as before."""
        app = make_app(pending_end_reason=None, start_time_source="live")
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertEqual(app.mobile_calls[0]["message"], LATE_PUSH)
        self.assertTrue(logged(
            app, "Finish detected 172 min late (> 20 min) - mobile push instead of Sonos announcement"
        ))
        rec = app.saved_feedback[0]
        self.assertEqual(rec["end_reason"], "low_power_detected")
        self.assertEqual(rec["duration_source"], "history_corrected")
        self.assertAlmostEqual(rec["duration_min"], 29.5, delta=0.01)  # last_high + 2 min
        self.assertAlmostEqual(rec["idle_min"], 170.0, delta=0.01)


class DurationFloor(unittest.TestCase):
    def test_5a_correct_duration_floors_history_end_at_trusted_tail(self):
        """_correct_duration directly: the energy history's only >100W interval ends at +27
        min; the trusted tail at +183 floors the corrected end there."""
        app = make_app(start_time_source="live", anti_crease_tail_since=TAIL_AT_183)
        app._history[app.energy_sensor] = energy_series(START, ECO_ENERGY)
        run_minutes, source = app._correct_duration(199.5)
        self.assertEqual(source, "history_corrected")
        self.assertAlmostEqual(run_minutes, 183.0, delta=0.01)
        self.assertTrue(logged(app, "anti-crease tail"))

    def test_5b_unemptied_transition_records_floored_duration_and_idle(self):
        """Through the real transition: feedback and entity carry the floored duration (~183)
        and idle (~16), not 27 / 172."""
        app = make_app(
            pending_end_reason="anti_crease_pattern",
            start_time_source="live",
            anti_crease_tail_since=TAIL_AT_183,
        )
        app._history[app.energy_sensor] = energy_series(START, ECO_ENERGY)
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        rec = app.saved_feedback[0]
        self.assertEqual(rec["duration_source"], "history_corrected")
        self.assertAlmostEqual(rec["duration_min"], 183.0, delta=0.01)
        self.assertAlmostEqual(rec["idle_min"], 16.5, delta=0.01)
        attrs = app.set_state_calls[-1]["attributes"]
        self.assertEqual(attrs["run_time_minutes"], 183.0)
        self.assertEqual(attrs["idle_min"], 16.5)
        self.assertEqual(app.sonos_calls, ["Washer is ready to be emptied"])

    def test_5c_history_branch_without_stamp_is_unchanged(self):
        """REGRESSION GUARD: no stamp -> the history end (+27.5) is used exactly as before."""
        app = make_app(start_time_source="live")
        app._history[app.energy_sensor] = energy_series(START, ECO_ENERGY)
        run_minutes, source = app._correct_duration(199.5)
        self.assertEqual(source, "history_corrected")
        self.assertAlmostEqual(run_minutes, 27.5, delta=0.01)
        self.assertFalse(logged(app, "anti-crease tail"))

    def test_6_door_opened_first_path_is_floored_too(self):
        """door_opened_first (door opened at +365 min, long after the +183 tail): the same floor
        applies on the door path - duration ~183, not 27."""
        now = START + timedelta(minutes=365)
        app = make_app(
            now=now,
            pending_end_reason="door_opened_first",
            start_time_source="live",
            anti_crease_tail_since=TAIL_AT_183,
            door_now="on",
            power_history=power_series(now, TAIL_SHAPE),
        )
        app._history[app.energy_sensor] = energy_series(START, ECO_ENERGY + [(300, 0.618), (364, 0.619)])
        # _handle_door_opened's past-addload sequence.
        app._transition_to_unemptied(skip_announce=True)
        app._transition_to_emptied("Door opened - emptying")
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(app.mobile_calls, [])
        self.assertEqual(len(app.saved_feedback), 1)
        rec = app.saved_feedback[0]
        self.assertEqual(rec["end_reason"], "door_opened_first")
        self.assertAlmostEqual(rec["duration_min"], 183.0, delta=0.01)
        self.assertAlmostEqual(rec["idle_min"], 182.0, delta=0.01)


class ReconcileOverrideWins(unittest.TestCase):
    def test_7a_latency_is_exactly_the_override_when_set(self):
        """With _finish_anchor_override set (the reconcile's D1 pin) and a fresh trusted stamp,
        latency is exactly what the override dictates; without it the stamp floors."""
        app = make_app(start_time_source="live", anti_crease_tail_since=NOW - timedelta(minutes=2))
        app._finish_anchor_override = NOW - timedelta(minutes=100)
        self.assertEqual(app._finish_detection_latency_minutes(), 100.0)
        app._finish_anchor_override = None
        self.assertEqual(app._finish_detection_latency_minutes(), 2.0)

    def test_7b_reconcile_with_trusted_pre_boot_stamp_still_pushes_never_sonos(self):
        """_restore_reconcile on an uncorroborated store restore whose payload carried a
        pre-boot tail stamp: the override still pins the latency (run - addload = 355 min),
        the forced push fires and Sonos never does (D1 + D2 unchanged). The duration floor is
        independent of the override: the pre-boot tail remains the best evidence of when the
        wash ended (~180 min, not the 4h-old last_high + 2 min)."""
        start = NOW - timedelta(hours=6)
        app = make_app(
            restored_uncorroborated=True,
            start=start,
            last_high_energy_at=NOW - timedelta(hours=4),
            anti_crease_tail_since=NOW - timedelta(hours=3),
            start_time_source="durable_store",
            app_started_at=NOW - timedelta(minutes=2),
            power_history=[{"state": "0.0", "last_changed": _iso(NOW - timedelta(minutes=m))}
                           for m in (9, 6, 3, 1)],
        )
        self.assertIsNotNone(app._trusted_anti_crease_tail_since())
        app._restore_reconcile({})
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertEqual(
            app.mobile_calls[0]["message"],
            "Washer finished about 355 min ago (late detection) - ready to empty.",
        )
        self.assertTrue(app.notification_sent)
        self.assertTrue(logged(app, "Push forced by the restore reconcile"))
        self.assertIsNone(app._finish_anchor_override)
        self.assertFalse(app._announce_force_push)
        rec = app.saved_feedback[0]
        self.assertEqual(rec["duration_source"], "history_corrected")
        self.assertAlmostEqual(rec["duration_min"], 180.0, delta=0.01)


class StampSiteAndStore(unittest.TestCase):
    def _anti_crease_app(self, now):
        """The FLAW 2 setup (test_washer_audit_2026_09): ~115/119 min into a user-confirmed
        Strygelet with an anti-crease tail shape in the last 10 min - reaches the anti-crease
        tail_ok branch but not the past-expected announce."""
        app = trs.make_full_init_app(
            sensor_state=None,
            helper_state=None,
            power_watts=0,
            now=NOW,
            extra_args={"confirm_entity": "input_select.washer_confirmed_programme"},
        )
        app.now = now
        app.states[app.confirm_entity] = "Strygelet"
        app.programme_confirmed_by_user = True
        app.state = "Running"
        app.states[app.state_entity] = "Running"
        app.start_time = now - timedelta(minutes=115)
        app.notification_sent = False
        app.last_door_closed_trusted = False
        app.energy_start = 0.0
        app.states[app.energy_sensor] = 0.35
        self._extend_tail(app, now)
        app.states[app.power_sensor] = "45.0"
        return app

    @staticmethod
    def _extend_tail(app, now):
        points = []
        t = now - timedelta(minutes=10)
        watt_cycle = [2.0, 2.0, 45.0, 3.0, 2.0, 45.0, 3.0, 2.0, 45.0, 3.0]
        i = 0
        while t < now - timedelta(seconds=5):
            points.append({"state": str(watt_cycle[i % len(watt_cycle)]), "last_changed": _iso(t)})
            t += timedelta(seconds=60)
            i += 1
        points.append({"state": "45.0", "last_changed": _iso(now - timedelta(seconds=5))})
        app._history[app.power_sensor] = points

    def test_anti_crease_tail_ok_tick_stamps_once_and_latches(self):
        """The real _check_energy_finish anti-crease branch stamps _anti_crease_tail_since with
        the tick's own clock on the first tail_ok tick and never moves it on later ones."""
        first = NOW + timedelta(minutes=1)
        app = self._anti_crease_app(first)
        self.assertIsNone(app._anti_crease_tail_since)
        self.assertEqual(app._app_started_at, NOW)

        app._check_energy_finish({})
        self.assertEqual(app.state, "Running")
        self.assertTrue(app.in_finishing_tail)
        self.assertEqual(app._anti_crease_tail_since, first)

        second = first + timedelta(minutes=1)
        app.now = second
        self._extend_tail(app, second)
        app._check_energy_finish({})
        self.assertEqual(app._anti_crease_tail_since, first)

    def test_store_round_trip_restores_stamp_as_pre_boot_trusted(self):
        """Persisted under anti_crease_tail_since and restored by _finalize_restored_cycle_identity;
        being older than this boot it is trusted although start_time is only durable_store."""
        tail = NOW - timedelta(minutes=12)
        payload = trs.make_store_payload(
            state="Running",
            start_time=NOW - timedelta(minutes=150),
            cycle_id="cycle-eco",
            anti_crease_tail_since=_iso(tail),
        )
        app = trs.make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0, store_payload=payload, now=NOW,
        )
        self.assertEqual(app.state, "Running")
        self.assertEqual(app._start_time_source, "durable_store")
        self.assertEqual(app._app_started_at, NOW)
        self.assertEqual(app._anti_crease_tail_since, tail)
        self.assertEqual(app._trusted_anti_crease_tail_since(), tail)
        self.assertEqual(app._build_cycle_store_payload("Running")["anti_crease_tail_since"], _iso(tail))

    def test_store_without_stamp_restores_none(self):
        payload = trs.make_store_payload(state="Running", start_time=NOW - timedelta(minutes=150))
        app = trs.make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0, store_payload=payload, now=NOW,
        )
        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._anti_crease_tail_since)
        self.assertIsNone(app._trusted_anti_crease_tail_since())
        self.assertEqual(app._build_cycle_store_payload("Running")["anti_crease_tail_since"], "")

    def test_cycle_boundaries_clear_the_latch(self):
        """A fresh cycle, an Off reset, an AddLoad resume and a false-finish recovery all drop
        the latch so it can never floor a later, unrelated finish."""
        stamp = NOW - timedelta(minutes=3)
        app = trs.make_full_init_app(sensor_state=None, helper_state=None, power_watts=0, now=NOW)

        app._anti_crease_tail_since = stamp
        app._reset_cycle_tracking()
        self.assertIsNone(app._anti_crease_tail_since)

        app._anti_crease_tail_since = stamp
        app._begin_running_cycle()
        self.assertIsNone(app._anti_crease_tail_since)

        app._anti_crease_tail_since = stamp
        app.state = "Paused"
        app.states[app.state_entity] = "Paused"
        app._transition_to_running_from_pause(force=True)
        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._anti_crease_tail_since)

        app._anti_crease_tail_since = stamp
        app.state = "Unemptied"
        app.states[app.state_entity] = "Unemptied"
        app.attrs_store.setdefault(app.state_entity, {})["run_time_minutes"] = 120
        app._recover_from_false_unemptied(60.0)
        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._anti_crease_tail_since)


if __name__ == "__main__":
    unittest.main()
