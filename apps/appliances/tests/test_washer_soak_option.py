# tests/test_washer_soak_option.py - the Iblødsætning (soak) option: HA helper wiring,
# supports_soak/ETA bonus, the toggle listener, the Off-transition reset, and the
# duration-learning exclusion (a soak cycle must not pollute the learned average).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

import json
import os
import sys
import tempfile
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
import washer_feedback as wfb  # noqa: E402

import test_washer_restart_survival as trs  # noqa: E402

NOW = datetime(2026, 9, 23, 14, 41, 0, tzinfo=timezone.utc)


# =============================================================================
# Pure functions (washer_feedback.py) - no AppDaemon/WasherMonitor involved.
# =============================================================================

class AggregateCyclesExcludesSoakDuration(unittest.TestCase):
    _PROFILES = {"bomuld": {"label": "Bomuld", "duration_min": 159, "heats": True}}

    def test_soak_cycle_excluded_from_durations_but_still_counted_for_accuracy(self):
        cycles = [
            {
                "confirmed": "bomuld", "confirmed_temperature": None,
                "predicted": "bomuld", "predicted_temperature": None,
                "duration_min": 159.0, "programme_user_confirmed": True, "valid_for_learning": True,
            },
            {
                "confirmed": "bomuld", "confirmed_temperature": None,
                "predicted": "bomuld", "predicted_temperature": None,
                "duration_min": 189.0, "programme_user_confirmed": True, "valid_for_learning": True,
                "selected_options": {"soak": "on"},
            },
        ]
        buckets, _centroids, skipped = wfb.aggregate_cycles(cycles, self._PROFILES)
        bucket = buckets["bomuld"]
        self.assertEqual(bucket["durations"], [159.0])
        self.assertEqual(bucket["total"], 2)
        self.assertEqual(bucket["correct"], 2)
        self.assertEqual(skipped, 0)

    def test_all_soak_leaves_an_empty_but_present_bucket(self):
        cycles = [
            {
                "confirmed": "bomuld", "confirmed_temperature": None,
                "predicted": "bomuld", "predicted_temperature": None,
                "duration_min": 189.0, "programme_user_confirmed": True, "valid_for_learning": True,
                "selected_options": {"soak": "on"},
            },
        ]
        buckets, _centroids, _skipped = wfb.aggregate_cycles(cycles, self._PROFILES)
        self.assertEqual(buckets["bomuld"]["durations"], [])
        self.assertEqual(buckets["bomuld"]["total"], 1)


class ApplyAndRemoveLearnedSampleSkipDuration(unittest.TestCase):
    def test_apply_skip_duration_leaves_learned_untouched_but_updates_centroid(self):
        learned: dict = {}
        centroids: dict = {}
        avg = wfb.apply_learned_sample(
            learned, centroids, "bomuld", 189.0, 0.9, 2, heats=True, skip_duration=True,
        )
        self.assertIsNone(avg)
        self.assertNotIn("bomuld", learned)
        self.assertIn("bomuld", centroids)
        self.assertAlmostEqual(centroids["bomuld"]["rate"], 0.9 / 189.0)

    def test_apply_without_skip_updates_both(self):
        learned = {"bomuld": {"n": 1, "avg": 159.0}}
        centroids: dict = {}
        avg = wfb.apply_learned_sample(learned, centroids, "bomuld", 165.0, 0.8, 2, heats=True)
        self.assertAlmostEqual(avg, (159.0 + 165.0) / 2)
        self.assertEqual(learned["bomuld"]["n"], 2)

    def test_remove_skip_duration_leaves_learned_untouched(self):
        learned = {"bomuld": {"n": 1, "avg": 159.0}}
        centroids = {"bomuld": {"rate": 0.9 / 189.0, "heating_bursts": 2.0, "n": 1}}
        wfb.remove_learned_sample(learned, centroids, "bomuld", 189.0, 0.9, 2, skip_duration=True)
        self.assertEqual(learned["bomuld"], {"n": 1, "avg": 159.0})
        self.assertNotIn("bomuld", centroids)  # centroid still backed out -> n dropped to 0

    def test_remove_without_skip_backs_out_the_sample(self):
        learned = {"bomuld": {"n": 2, "avg": (159.0 + 165.0) / 2}}
        centroids: dict = {}
        wfb.remove_learned_sample(learned, centroids, "bomuld", 165.0, 0.8, 2)
        self.assertEqual(learned["bomuld"]["n"], 1)
        self.assertAlmostEqual(learned["bomuld"]["avg"], 159.0)


# =============================================================================
# WasherMonitor helpers - real washer_programmes.yaml via _load_programme_profiles.
# =============================================================================

class ProgrammeSupportsSoakAndBonusMinutes(unittest.TestCase):
    """Exercises the REAL WasherMonitor._programme_supports_soak/_soak_bonus_minutes against
    the real washer_programmes.yaml, not a mirror - same pattern as
    TestEcoPerTemperatureLearning in test_washer_completion.py."""

    def setUp(self):
        self.app = wm.WasherMonitor.__new__(wm.WasherMonitor)
        self.app.args = {}
        self.app.log = lambda *a, **kw: None
        self.app._load_programme_profiles()

    def test_soak_duration_min_loaded_from_yaml(self):
        self.assertEqual(self.app._soak_duration_min, 30)

    def test_supports_soak_true_for_programmes_listing_it(self):
        # Regression guard: bomuld/eco/strygelet/finvask all have by_temperature, and
        # _get_profile would strip a programme-level supports_soak for those - this must read
        # the top-level profile directly instead.
        for prog in ("bomuld", "eco", "strygelet", "finvask"):
            self.assertTrue(self.app._programme_supports_soak(prog), prog)

    def test_supports_soak_false_for_others(self):
        for prog in (
            "uld", "ekspres", "morkt_denim", "outdoor",
            "impraegnering", "pumpe_centrifugering", "kun_skyl_stivelse", "unknown", None,
        ):
            self.assertFalse(self.app._programme_supports_soak(prog), prog)

    def test_bonus_zero_without_entity_configured(self):
        self.app.option_soak_entity = None
        self.assertEqual(self.app._soak_bonus_minutes("bomuld"), 0)

    def test_bonus_zero_when_helper_off(self):
        self.app.option_soak_entity = "input_boolean.washer_option_soak"
        self.app.get_state = lambda entity: "off"
        self.assertEqual(self.app._soak_bonus_minutes("bomuld"), 0)

    def test_bonus_is_soak_duration_min_when_on_and_supported(self):
        self.app.option_soak_entity = "input_boolean.washer_option_soak"
        self.app.get_state = lambda entity: "on"
        self.assertEqual(self.app._soak_bonus_minutes("bomuld"), 30)

    def test_bonus_zero_when_on_but_programme_unsupported(self):
        self.app.option_soak_entity = "input_boolean.washer_option_soak"
        self.app.get_state = lambda entity: "on"
        self.assertEqual(self.app._soak_bonus_minutes("uld"), 0)


# =============================================================================
# End to end via make_full_init_app - real initialize()/_restore_running_state, only the
# AppDaemon surface is faked (see test_washer_restart_survival.py's own CRITICAL rules).
# =============================================================================

def _running_bomuld_app(**extra_args):
    stored_start = NOW - timedelta(minutes=30)
    payload = trs.make_store_payload(
        state="Running", start_time=stored_start, cycle_id="cycle-soak-eta",
        detected_programme="bomuld", detected_temperature="30°C", energy_at_start=0.10,
    )
    args = {
        "option_soak_entity": "input_boolean.washer_option_soak",
        "confirm_entity": "input_select.washer_confirmed_programme",
        "temperature_entity": "input_select.washer_temperature",
    }
    args.update(extra_args)
    app = trs.make_full_init_app(
        sensor_state=None, helper_state="Off", power_watts=200,
        store_payload=payload, now=NOW, extra_args=args,
    )
    app.states["input_select.washer_confirmed_programme"] = "Bomuld"
    app.states["input_select.washer_temperature"] = "30°C"
    return app


class SoakEtaEndToEnd(unittest.TestCase):
    def test_soak_on_adds_bonus_to_published_eta(self):
        app = _running_bomuld_app()

        app.states["input_boolean.washer_option_soak"] = "off"
        app._push_running_eta_attributes()
        dur_off = trs.last_publish(app)["attributes"]["programme_duration_min"]
        self.assertTrue(trs.last_publish(app)["attributes"]["supports_soak"])

        app.states["input_boolean.washer_option_soak"] = "on"
        app._push_running_eta_attributes()
        pub_on = trs.last_publish(app)["attributes"]
        self.assertEqual(pub_on["programme_duration_min"] - dur_off, 30)
        self.assertTrue(pub_on["supports_soak"])

    def test_soak_bonus_absent_when_programme_does_not_support_it(self):
        app = _running_bomuld_app()
        app.states["input_select.washer_confirmed_programme"] = "Uld"
        app.states["input_select.washer_temperature"] = "30°C"
        app.states["input_boolean.washer_option_soak"] = "on"
        app._push_running_eta_attributes()
        attrs = trs.last_publish(app)["attributes"]
        self.assertFalse(attrs["supports_soak"])


class SoakOptionChangeListener(unittest.TestCase):
    def test_toggle_while_running_pushes_eta_immediately(self):
        app = _running_bomuld_app()
        app.states["input_boolean.washer_option_soak"] = "off"
        app._push_running_eta_attributes()
        dur_off = trs.last_publish(app)["attributes"]["programme_duration_min"]

        app.states["input_boolean.washer_option_soak"] = "on"
        app._on_soak_option_changed("input_boolean.washer_option_soak", "state", "off", "on", {})

        dur_on = trs.last_publish(app)["attributes"]["programme_duration_min"]
        self.assertEqual(dur_on - dur_off, 30)

    def test_ignored_when_not_running(self):
        app = trs.make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0,
            extra_args={"option_soak_entity": "input_boolean.washer_option_soak"},
        )
        self.assertNotEqual(app.state, "Running")
        before = len(app.set_state_calls)
        app._on_soak_option_changed("input_boolean.washer_option_soak", "state", "off", "on", {})
        self.assertEqual(len(app.set_state_calls), before)


class SoakResetOnCycleEnd(unittest.TestCase):
    def test_transition_to_off_turns_off_the_soak_helper(self):
        stored_start = NOW - timedelta(minutes=200)
        payload = trs.make_store_payload(
            state="Running", start_time=stored_start, cycle_id="cycle-soak-off",
            detected_programme="bomuld", detected_temperature="30°C",
        )
        app = trs.make_full_init_app(
            sensor_state=None, helper_state="Off", power_watts=200, store_payload=payload, now=NOW,
            extra_args={"option_soak_entity": "input_boolean.washer_option_soak"},
        )
        self.assertEqual(app.state, "Running")
        calls = []
        app.call_service = lambda service, **kw: calls.append((service, kw))

        app._transition_to_off("test", force=True)

        self.assertEqual(app.state, "Off")
        self.assertIn(
            ("input_boolean/turn_off", {"entity_id": "input_boolean.washer_option_soak"}),
            calls,
        )

    def test_no_call_when_entity_not_configured(self):
        stored_start = NOW - timedelta(minutes=200)
        payload = trs.make_store_payload(
            state="Running", start_time=stored_start, cycle_id="cycle-soak-off-2",
            detected_programme="bomuld", detected_temperature="30°C",
        )
        app = trs.make_full_init_app(
            sensor_state=None, helper_state="Off", power_watts=200, store_payload=payload, now=NOW,
        )
        calls = []
        app.call_service = lambda service, **kw: calls.append((service, kw))
        app._transition_to_off("test", force=True)
        self.assertFalse(any(service == "input_boolean/turn_off" for service, _kw in calls))


class SaveAndRemoveCycleFeedbackSoakGating(unittest.TestCase):
    def _app_with_feedback_file(self):
        tmpdir = tempfile.mkdtemp(prefix="washer_soak_feedback_")
        feedback_file = os.path.join(tmpdir, "washer_feedback.json")
        return trs.make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0,
            extra_args={"feedback_file": feedback_file},
        )

    def _save(self, app, duration_min, soak=False):
        return app._save_cycle_feedback(
            predicted="bomuld", predicted_temperature="30°C",
            confirmed="bomuld", confirmed_temperature="30°C",
            duration_min=duration_min, energy_kwh=0.75,
            heating_bursts=2, max_power_w=2000.0,
            user_confirmed=True,
            completion_class="completed", valid_for_learning=True, validation_flags=[],
            selected_options={"soak": "on"} if soak else None,
        )

    def test_soak_cycle_does_not_join_the_learned_average(self):
        app = self._app_with_feedback_file()
        self._save(app, 159.0)
        self.assertEqual(app._learned_durations["bomuld|30°C"]["n"], 1)
        self.assertAlmostEqual(app._learned_durations["bomuld|30°C"]["avg"], 159.0)

        self._save(app, 189.0, soak=True)
        # n/avg unchanged by the soak cycle's inflated duration.
        self.assertEqual(app._learned_durations["bomuld|30°C"]["n"], 1)
        self.assertAlmostEqual(app._learned_durations["bomuld|30°C"]["avg"], 159.0)

    def test_removing_a_soak_cycle_does_not_corrupt_the_average(self):
        app = self._app_with_feedback_file()
        self._save(app, 159.0)
        self._save(app, 189.0, soak=True)
        self.assertEqual(app._learned_durations["bomuld|30°C"]["n"], 1)

        app._remove_last_cycle_feedback()  # retracts the just-saved (soak) cycle

        self.assertEqual(app._learned_durations["bomuld|30°C"]["n"], 1)
        self.assertAlmostEqual(app._learned_durations["bomuld|30°C"]["avg"], 159.0)

    def test_non_soak_cycles_still_learn_normally(self):
        app = self._app_with_feedback_file()
        self._save(app, 155.0)
        self._save(app, 165.0)
        self.assertEqual(app._learned_durations["bomuld|30°C"]["n"], 2)
        self.assertAlmostEqual(app._learned_durations["bomuld|30°C"]["avg"], 160.0)


class LoadAndApplyFeedbackAllSoakBucket(unittest.TestCase):
    def test_all_soak_cycles_do_not_crash_and_leave_no_learned_entry(self):
        tmpdir = tempfile.mkdtemp(prefix="washer_soak_reload_")
        feedback_file = os.path.join(tmpdir, "washer_feedback.json")
        with open(feedback_file, "w") as f:
            json.dump(
                {
                    "version": 2,
                    "cycles": [
                        {
                            "ts": "2026-09-01T10:00:00+02:00",
                            "confirmed": "bomuld", "confirmed_temperature": "30",
                            "predicted": "bomuld", "predicted_temperature": "30",
                            "duration_min": 189.0, "energy_kwh": 0.75, "heating_bursts": 2,
                            "programme_user_confirmed": True, "valid_for_learning": True,
                            "selected_options": {"soak": "on"},
                        },
                    ],
                },
                f,
            )
        app = trs.make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0,
            extra_args={"feedback_file": feedback_file},
        )
        app._load_and_apply_feedback()  # must not raise ZeroDivisionError
        self.assertNotIn("bomuld|30°C", app._learned_durations)


if __name__ == "__main__":
    unittest.main()
