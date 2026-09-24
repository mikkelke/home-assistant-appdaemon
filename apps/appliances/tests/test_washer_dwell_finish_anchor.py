# tests/test_washer_dwell_finish_anchor.py - the final spin's end as the finish anchor on long-dwell
# programmes. Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# An Eco programme dwells for hours under energy_active_watts, so "the last high sample" can sit
# hours before the real end. Two consumers must not read such a timestamp as "when the wash ended":
#   _finish_detection_latency_minutes -> Sonos downgraded to a false "finished N min ago" push
#   _correct_duration                 -> duration / idle_min / the learned record hours short
# A spin-end finish carries its own anchor (_spin_end_at, the end of the final spin that the
# plug's reads showed); a standby finish anchors on last_high_energy_at, which HA power reports
# above energy_active_watts keep current - the final spin stamps it.
#
# Reuses make_finish_app (test_washer_door_aware_finish.py) for the finish transition and
# make_full_init_app (test_washer_restart_survival.py) for the cycle-boundary clears - only the
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

START = NOW - timedelta(minutes=199.5)
LAST_HIGH = NOW - timedelta(minutes=172)      # a dwell phase that began hours before the end
SPIN_END = START + timedelta(minutes=196.5)   # the final spin ended 3 min before the decision
FRESHNESS = 20                                # washer.yaml announce_freshness_minutes
LATE_PUSH = "Washer finished about 172 min ago (late detection) - ready to empty."

TAIL_SHAPE = [3.0, 40.0, 3.0, 38.0, 3.0, 41.0, 3.0, 2.0]


def power_series(now, values, spacing_s=60):
    n = len(values)
    return [
        {"state": str(w), "last_changed": _iso(now - timedelta(seconds=spacing_s * (n - i)))}
        for i, w in enumerate(values)
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


class SpinEndAnchorsLatency(unittest.TestCase):
    def test_spin_end_finish_is_fresh_although_last_high_is_hours_old(self):
        app = make_app(pending_end_reason="spin_end", start_time_source="live")
        app._spin_end_at = SPIN_END
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, ["Washer is ready to be emptied"])
        self.assertEqual(app.mobile_calls, [])
        self.assertFalse(logged(app, "mobile push instead of Sonos"))
        rec = app.saved_feedback[0]
        self.assertEqual(rec["end_reason"], "spin_end")
        self.assertEqual(rec["duration_source"], "spin_end")
        self.assertAlmostEqual(rec["duration_min"], 196.5, delta=0.01)

    def test_standby_finish_with_an_old_anchor_is_a_late_push(self):
        """Without a spin stamp the last high sample is the anchor; hours old means late: push."""
        app = make_app(pending_end_reason="standby", start_time_source="live")
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.mobile_calls), 1)
        self.assertEqual(app.mobile_calls[0]["message"], LATE_PUSH)
        self.assertTrue(app.notification_sent)


class SpinEndAnchorsDuration(unittest.TestCase):
    def test_correct_duration_uses_the_spin_end(self):
        app = make_app(start_time_source="live")
        app._spin_end_at = SPIN_END
        self.assertEqual(app._correct_duration(199.5), (196.5, "spin_end"))

    def test_door_opened_first_path_uses_the_spin_end_when_known(self):
        now = START + timedelta(minutes=365)
        app = make_app(now=now, pending_end_reason="door_opened_first", start_time_source="live",
                       door_now="on", power_history=power_series(now, TAIL_SHAPE))
        app._spin_end_at = SPIN_END
        app._transition_to_unemptied(skip_announce=True)
        app._transition_to_emptied("Door opened - emptying")
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])
        self.assertEqual(len(app.saved_feedback), 1)
        self.assertAlmostEqual(app.saved_feedback[0]["duration_min"], 196.5, delta=0.01)


class LastHighFollowsPowerReports(unittest.TestCase):
    def test_reports_above_active_watts_stamp_last_high_while_running(self):
        app = trs.make_full_init_app(sensor_state=None, helper_state=None, power_watts=0, now=NOW)
        app.state = "Running"
        app.states[app.state_entity] = "Running"
        app.last_high_energy_at = LAST_HIGH
        app._power_changed(app.power_sensor, "state", "3.0", "40.0", {})
        self.assertEqual(app.last_high_energy_at, LAST_HIGH)
        app._power_changed(app.power_sensor, "state", "40.0", "450.0", {})
        self.assertEqual(app.last_high_energy_at, NOW)


class CycleBoundariesClearTheSpinEnd(unittest.TestCase):
    def test_fresh_cycle_reset_and_recovery_drop_it(self):
        app = trs.make_full_init_app(sensor_state=None, helper_state=None, power_watts=0, now=NOW)

        app._spin_end_at = SPIN_END
        app._reset_cycle_tracking()
        self.assertIsNone(app._spin_end_at)

        app._spin_end_at = SPIN_END
        app._begin_running_cycle()
        self.assertIsNone(app._spin_end_at)

        app._spin_end_at = SPIN_END
        app.state = "Unemptied"
        app.states[app.state_entity] = "Unemptied"
        app.attrs_store.setdefault(app.state_entity, {})["run_time_minutes"] = 120
        app._recover_from_false_unemptied(60.0)
        self.assertEqual(app.state, "Running")
        self.assertIsNone(app._spin_end_at)


if __name__ == "__main__":
    unittest.main()
