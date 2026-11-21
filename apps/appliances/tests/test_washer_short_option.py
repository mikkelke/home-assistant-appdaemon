# tests/test_washer_short_option.py - the Kort (shorter wash) option: which programmes have it, the
# classification of a Kort wash, its own ETA history, the ETA of a run with Kort on, the helper wiring
# (listener, programme change, Off reset) and the marking of a wash that has already finished.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

import json
import os
import tempfile
import unittest
from datetime import timedelta

import test_washer_restart_survival as trs  # installs the appdaemon stub and the app directory on sys.path

import run_profile_model as model  # noqa: E402
import washer_classify as wcls  # noqa: E402
import washer_eta as weta  # noqa: E402
import washer_feedback as wfb  # noqa: E402
import washer_monitor as wm  # noqa: E402
import washer_profiles as wp  # noqa: E402

START = trs.NOW
ENERGY = "sensor.washer_plug_energy"
ENERGY_START = 0.10
CONFIRM = "input_select.washer_confirmed_programme"
TEMPERATURE = "input_select.washer_temperature"
SHORT = "input_boolean.washer_option_short"
SOAK = "input_boolean.washer_option_soak"
STATE = "sensor.washer_state"

WITH_KORT = ("bomuld", "strygelet", "finvask", "morkt_denim", "outdoor")
WITHOUT_KORT = ("ekspres", "eco", "uld", "impraegnering", "pumpe_centrifugering", "kun_skyl_stivelse", "drum_clean", "unknown")


def record(day, duration, kwh, **extra):
    rec = {"ts": f"2026-10-{day:02d}T10:00:00+00:00", "duration_min": duration, "energy_kwh": kwh,
           "completion_class": "completed"}
    rec.update(extra)
    return rec


def kort(day, duration, programme="bomuld", temperature="60", options=None, **extra):
    return record(day, duration, 1.0, confirmed=programme, confirmed_temperature=temperature,
                  programme_user_confirmed=True, selected_options=options or {"short": "on"}, **extra)


def raw(path):
    with open(path) as f:
        return f.read()


def profile(rec):
    start = model.parse_epoch(rec["ts"]) - rec["duration_min"] * 60
    minutes = int(rec["duration_min"])
    return {"status": "ok", "cst": start, "onset": start - 6, "end": start + rec["duration_min"] * 60,
            "unavail_s": 0.0, "dwh": [round(rec["energy_kwh"] * 10000 / minutes)] * minutes}


LIGHT = [record(day, 60, 0.3) for day in (1, 2, 3, 4)]
HEAVY = [record(day, 180, 1.8) for day in (5, 6, 7, 8)]
PLAIN = LIGHT + HEAVY
KORT_BOMULD_60 = [kort(10, 100), kort(11, 110), kort(12, 120)]


class WhichProgrammesHaveKort(unittest.TestCase):
    """Manual: Kort is a switch on Bomuld, Strygelet, Finvask, Mørkt/Denim and Outdoor; Ekspres 20 has it built in; ECO,
    Uld, Imprægnering and Kun skyl/stivelse do not offer it."""

    def setUp(self):
        self.app = wm.WasherMonitor.__new__(wm.WasherMonitor)
        self.app.args = {}
        self.app.log = lambda *a, **kw: None
        self.app._load_programme_profiles()

    def test_the_programme_file_offers_it_where_the_manual_does(self):
        for key in WITH_KORT:
            self.assertTrue(wp.supports_short(self.app.PROGRAMME_PROFILES, key), key)
            self.assertIn("short", self.app.PROGRAMME_PROFILES[key]["available_options"], key)
        for key in WITHOUT_KORT:
            self.assertFalse(wp.supports_short(self.app.PROGRAMME_PROFILES, key), key)
            self.assertNotIn("short", self.app.PROGRAMME_PROFILES[key].get("available_options", []), key)

    def test_the_built_in_tables_agree_when_the_file_cannot_be_read(self):
        for key in WITH_KORT:
            self.assertTrue(wp.supports_short(wp.DEFAULT_PROFILES, key), key)
        for key in WITHOUT_KORT:
            self.assertFalse(wp.supports_short(wp.DEFAULT_PROFILES, key), key)

    def test_a_programme_nobody_has_heard_of_has_no_kort(self):
        self.assertFalse(wp.supports_short(self.app.PROGRAMME_PROFILES, "nope"))
        self.assertFalse(wp.supports_short(self.app.PROGRAMME_PROFILES, None))


class AKortWashIsNeverAPlainSample(unittest.TestCase):
    def setUp(self):
        self.app = wm.WasherMonitor.__new__(wm.WasherMonitor)
        self.app.args = {}
        self.app.log = lambda *a, **kw: None
        self.app._load_programme_profiles()
        self.app.programme_confirmed_by_user = True
        self.app.completion_guard_fraction = 0.65
        self.app.completion_guard_fraction_user_confirmed = 0.60
        self.app.min_cycle_minutes = 25
        self.app.min_energy_kwh = 0.1

    def classify(self, minutes, options):
        return self.app._classify_cycle_completion(
            run_minutes=minutes, energy_kwh=1.0, heating_bursts=2, max_power_w=2000.0,
            predicted="bomuld", predicted_temperature="60°C", confirmed="bomuld", confirmed_temperature="60°C",
            transition_path="spin_end", selected_options=options)

    def test_kort_is_flagged_and_not_learnable(self):
        c = self.classify(120.0, {"short": "on"})
        self.assertFalse(c["valid_for_learning"])
        self.assertIn("short_selected", c["validation_flags"])
        self.assertEqual(c["completion_class"], "completed")
        self.assertTrue(self.classify(120.0, {"short": "off"})["valid_for_learning"])
        self.assertTrue(self.classify(120.0, None)["valid_for_learning"])

    def test_a_kort_wash_is_not_too_short_for_having_been_shorter_than_the_table(self):
        plain = self.classify(70.0, None)
        self.assertIn("runtime_too_short", plain["validation_flags"])
        self.assertEqual(plain["completion_class"], "suspect")
        short = self.classify(70.0, {"short": "on"})
        self.assertEqual(short["validation_flags"], ["short_selected"])
        self.assertEqual(short["completion_class"], "completed")

    def test_a_run_below_the_minimum_cycle_is_still_interrupted(self):
        self.assertEqual(self.classify(20.0, {"short": "on"})["completion_class"], "interrupted")

    def test_migration_quarantines_an_older_kort_record_marked_eligible(self):
        cycles = [{
            "confirmed": "bomuld", "confirmed_temperature": "60", "predicted": "bomuld", "predicted_temperature": "60",
            "duration_min": 120.0, "energy_kwh": 1.0, "heating_bursts": 2, "max_power_w": 2000,
            "programme_user_confirmed": True, "valid_for_learning": True, "completion_class": "completed",
            "profile_version": "1", "validation_version": "2", "selected_options": {"short": "on"},
        }]
        wfb.migrate_records(cycles, self.app._classify_cycle_completion)
        self.assertFalse(cycles[0]["valid_for_learning"])
        buckets, centroids, _skipped = wfb.aggregate_cycles(cycles, self.app.PROGRAMME_PROFILES)
        self.assertFalse(any(b["durations"] for b in buckets.values()))
        self.assertFalse(centroids)

    def test_classification_of_ekspres_is_untouched(self):
        c = wcls.classify_cycle_completion(
            run_minutes=22.0, energy_kwh=0.3, confirmed="ekspres", transition_path="spin_end",
            profile=self.app._get_profile("ekspres", "40°C"), user_conf=True, guard_fraction=0.6,
            min_cycle_minutes=25, min_energy_kwh=0.1, validation_key="ekspres|40°C")
        self.assertNotIn("short_selected", c["validation_flags"])


class EligibilityOfKortRecords(unittest.TestCase):
    def test_kort_is_its_own_reason_and_soak_comes_first(self):
        base = record(1, 120, 1.0)
        self.assertIsNone(model.record_eligibility({**base, "selected_options": {"short": "off"}}))
        self.assertEqual(model.record_eligibility({**base, "selected_options": {"short": "on"}}), model.SHORT)
        self.assertEqual(model.record_eligibility({**base, "selected_options": {"short": "on", "soak": "on"}}), "soak")
        self.assertEqual(model.record_eligibility({**base, "completion_class": "interrupted",
                                                   "selected_options": {"short": "on"}}), "interrupted")


class EtaHistoryKeepsKortApart(unittest.TestCase):
    def key_of(self, rec):
        return f"{rec['confirmed']}|{rec['confirmed_temperature']}"

    def test_the_plain_history_leaves_kort_washes_out(self):
        cycles = PLAIN + KORT_BOMULD_60
        history = weta.eta_history(cycles, {})
        self.assertEqual(sorted(d for d, _ in history), [60.0] * 4 + [180.0] * 4)

    def test_the_kort_history_holds_only_kort_washes_by_key(self):
        cycles = PLAIN + KORT_BOMULD_60 + [kort(13, 70, "strygelet", "30"), kort(14, 80, "bomuld", "40"),
                                           kort(15, 90), record(16, 90, 1.0, selected_options={"short": "on"})]
        by_key = weta.short_eta_history(cycles, {}, lambda rec: self.key_of(rec) if rec.get("confirmed") else None)
        self.assertEqual(sorted(by_key), ["bomuld|40", "bomuld|60", "strygelet|30"])
        self.assertEqual(sorted(d for d, _ in by_key["bomuld|60"]), [90.0, 100.0, 110.0, 120.0])
        self.assertEqual(by_key["bomuld|40"], [(80.0, None)])

    def test_a_kort_wash_with_soak_or_interrupted_or_unreal_is_in_neither(self):
        cycles = [kort(10, 100, options={"short": "on", "soak": "on"}), kort(11, 100, completion_class="interrupted"),
                  kort(12, 10), kort(13, 400)]
        self.assertEqual(weta.eta_history(cycles, {}), [])
        self.assertEqual(weta.short_eta_history(cycles, {}, lambda rec: "k"), {})

    def test_a_kort_wash_extracted_before_it_was_marked_keeps_its_marks(self):
        rec = kort(10, 100)
        entries = {model.cache_key("washer", rec): profile(rec)}
        (_, marks), = weta.short_eta_history([rec], entries, lambda r: "k")["k"]
        self.assertEqual(len(marks), 20)


class EstimateWithKort(unittest.TestCase):
    def setUp(self):
        self.plain = weta.eta_history(PLAIN, {})
        self.short = weta.short_eta_history(PLAIN + KORT_BOMULD_60, {}, lambda rec: "bomuld|60" if rec.get("confirmed") else None)

    def test_the_kort_washes_of_the_key_that_lasted_longer_decide(self):
        self.assertEqual(weta.typical_total_with_short(self.short, self.plain, "bomuld|60", [], 0.0, 5.0), 110)
        self.assertEqual(weta.typical_total_with_short(self.short, self.plain, "bomuld|60", [], 0.0, 15.0), 110)

    def test_two_longer_kort_washes_are_too_few(self):
        self.assertEqual(weta.typical_total_with_short(self.short, self.plain, "bomuld|60", [], 0.0, 105.0), 180)

    def test_without_enough_kort_washes_an_unconfirmed_run_is_the_measure(self):
        self.assertEqual(weta.typical_total_with_short(self.short, self.plain, "bomuld|40", [], 0.0, 5.0),
                         weta.typical_total_after(self.plain, [], 0.0, 5.0))
        self.assertEqual(weta.typical_total_with_short({}, self.plain, "bomuld|60", [], 0.0, 5.0), 120)
        two = {"bomuld|60": self.short["bomuld|60"][:2]}
        self.assertEqual(weta.typical_total_with_short(two, self.plain, "bomuld|60", [], 0.0, 5.0), 120)

    def test_nothing_when_neither_history_can_tell(self):
        self.assertIsNone(weta.typical_total_with_short({}, [], "bomuld|60", [], 0.0, 5.0))


def _store_for_monitor(tmp):
    path = os.path.join(tmp, "run_profiles_cache_state.json")
    with open(path, "w") as f:
        json.dump({"version": 1, "entries": {model.cache_key("washer", r): profile(r) for r in PLAIN}}, f)
    return path


class MonitorTestCase(unittest.TestCase):
    cycles = PLAIN

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def running_app(self, programme="Bomuld", temperature="60°C", kort_helper="off", confirmed=True, cycles=None, **extra_args):
        payload = trs.make_store_payload(
            state="Running", start_time=START, cycle_id="cycle-kort", detected_programme="bomuld",
            detected_temperature="60°C", energy_at_start=ENERGY_START)
        args = {"confirm_entity": CONFIRM, "temperature_entity": TEMPERATURE, "option_short_entity": SHORT,
                "option_soak_entity": SOAK}
        args.update(extra_args)
        app = trs.make_full_init_app(
            sensor_state=None, helper_state="Off", power_watts=200, store_payload=payload, now=START, extra_args=args)
        app.states[CONFIRM] = programme if confirmed else "Auto (unconfirmed)"
        app.states[TEMPERATURE] = temperature
        app.states[SHORT] = kort_helper
        app.states[SOAK] = "off"
        app.programme_confirmed_by_user = confirmed
        app.RUN_PROFILES_CACHE = _store_for_monitor(self.tmp)
        app._reload_learning(self.cycles if cycles is None else cycles)
        return app

    def tick(self, app, minutes, kwh):
        app.now = START + timedelta(minutes=minutes)
        app.states[ENERGY] = ENERGY_START + kwh
        app._check_energy_finish({})
        return trs.last_publish(app)["attributes"]

    def run_to(self, app, minutes, per_min):
        for t in [*range(3, minutes, 4), minutes]:
            attrs = self.tick(app, t, per_min * t)
        return attrs


class WhetherKortIsOn(MonitorTestCase):
    def test_it_is_on_for_a_programme_that_has_it_when_the_helper_is_on(self):
        app = self.running_app(kort_helper="on")
        self.assertTrue(app._short_on("bomuld"))
        self.assertTrue(app._short_on("outdoor"))
        for key in ("ekspres", "eco", "uld", "unknown", None):
            self.assertFalse(app._short_on(key), key)

    def test_it_is_off_when_the_helper_is_off_or_missing(self):
        app = self.running_app(kort_helper="off")
        self.assertFalse(app._short_on("bomuld"))
        app.states[SHORT] = "unavailable"
        self.assertFalse(app._short_on("bomuld"))
        app.option_short_entity = None
        app.states[SHORT] = "on"
        self.assertFalse(app._short_on("bomuld"))

    def test_the_record_carries_it_only_for_a_confirmed_programme_that_has_it(self):
        app = self.running_app(kort_helper="on")
        self.assertEqual(app._get_selected_options(), {"soak": "off", "short": "on"})
        app.states[CONFIRM] = "Uld"
        self.assertEqual(app._get_selected_options()["short"], "off")
        app.states[CONFIRM] = "Ekspres"
        self.assertEqual(app._get_selected_options()["short"], "off")
        app.states[CONFIRM] = "Auto (unconfirmed)"
        self.assertEqual(app._get_selected_options()["short"], "off")
        app.states[CONFIRM] = "Strygelet"
        self.assertEqual(app._get_selected_options()["short"], "on")

    def test_the_record_says_off_when_the_helper_is_off(self):
        app = self.running_app(kort_helper="off")
        self.assertEqual(app._get_selected_options()["short"], "off")


class TheEtaOfARunWithKort(MonitorTestCase):
    cycles = PLAIN

    def test_without_kort_the_confirmed_programme_gives_its_table_duration(self):
        app = self.running_app(kort_helper="off")
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], 149)

    def test_with_kort_and_no_kort_history_it_is_what_an_unconfirmed_run_expects(self):
        app = self.running_app(kort_helper="on")
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], weta.typical_total_after(app._eta_history, app._eta_marks, 0.21, 21.0))
        self.assertEqual(attrs["programme_duration_min"], 180)
        self.assertNotEqual(attrs["programme_duration_min"], 149)

    def test_with_kort_history_it_is_the_length_of_those_washes(self):
        app = self.running_app(kort_helper="on", cycles=PLAIN + KORT_BOMULD_60)
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], 110)
        self.assertEqual(attrs["estimated_remaining_min"], 110 - 21)

    def test_the_kort_history_of_another_temperature_does_not_count(self):
        app = self.running_app(temperature="40°C", kort_helper="on", cycles=PLAIN + KORT_BOMULD_60)
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], 180)

    def test_kort_washes_do_not_move_the_unconfirmed_estimate(self):
        app = self.running_app(confirmed=False, cycles=PLAIN + KORT_BOMULD_60)
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], 180)
        self.assertEqual(weta.typical_total_after(app._eta_history, [], 0.0, 0), 120)

    def test_the_bomuld_tail_is_not_asked_for_under_kort(self):
        asked = []
        app = self.running_app(kort_helper="on")
        app._tail_eta_utc = lambda programme, now: asked.append(programme)
        self.tick(app, 21, 0.21)
        app._push_running_eta_attributes()
        self.assertEqual(asked, [None, None])
        asked.clear()
        app.states[SHORT] = "off"
        self.tick(app, 25, 0.25)
        app._push_running_eta_attributes()
        self.assertEqual(asked, ["bomuld", "bomuld"])

    def test_a_programme_without_kort_ignores_the_helper(self):
        app = self.running_app(programme="ECO", temperature="40-60°C", kort_helper="on")
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], app._get_programme_duration("eco", "40-60°C", use_learned=False))

    def test_the_immediate_push_follows_the_same_rule(self):
        app = self.running_app(kort_helper="on", cycles=PLAIN + KORT_BOMULD_60)
        app.now = START + timedelta(minutes=21)
        app.states[ENERGY] = ENERGY_START + 0.21
        app._push_running_eta_attributes()
        self.assertEqual(trs.last_publish(app)["attributes"]["programme_duration_min"], 110)
        app.states[SHORT] = "off"
        app._push_running_eta_attributes()
        self.assertEqual(trs.last_publish(app)["attributes"]["programme_duration_min"], 149)

    def test_soak_still_adds_its_time_on_top(self):
        app = self.running_app(kort_helper="on", cycles=PLAIN + KORT_BOMULD_60)
        app.states[SOAK] = "on"
        attrs = self.run_to(app, 21, 0.01)
        self.assertEqual(attrs["programme_duration_min"], 110 + 30)


class TheKortHelperWiring(MonitorTestCase):
    def test_toggling_it_while_running_pushes_the_eta_at_once(self):
        app = self.running_app(kort_helper="off", cycles=PLAIN + KORT_BOMULD_60)
        app.now = START + timedelta(minutes=21)
        app.states[ENERGY] = ENERGY_START + 0.21
        app._push_running_eta_attributes()
        self.assertEqual(trs.last_publish(app)["attributes"]["programme_duration_min"], 149)
        app.states[SHORT] = "on"
        app._on_short_option_changed(SHORT, "state", "off", "on", {})
        self.assertEqual(trs.last_publish(app)["attributes"]["programme_duration_min"], 110)

    def test_a_dropped_or_unknown_state_is_ignored(self):
        app = self.running_app(kort_helper="on")
        before = len(app.set_state_calls)
        app._on_short_option_changed(SHORT, "state", "on", "unavailable", {})
        self.assertEqual(len(app.set_state_calls), before)

    def test_a_programme_without_kort_switches_it_off(self):
        app = self.running_app(kort_helper="on")
        calls = []
        app.call_service = lambda service, **kw: calls.append((service, kw))
        app._apply_programme_ui_dropdowns("uld")
        self.assertIn(("input_boolean/turn_off", {"entity_id": SHORT}), calls)

    def test_a_programme_with_kort_leaves_it_alone(self):
        app = self.running_app(kort_helper="on")
        calls = []
        app.call_service = lambda service, **kw: calls.append((service, kw))
        app._apply_programme_ui_dropdowns("strygelet")
        self.assertNotIn(("input_boolean/turn_off", {"entity_id": SHORT}), calls)

    def test_nothing_is_switched_off_that_is_already_off(self):
        app = self.running_app(kort_helper="off")
        calls = []
        app.call_service = lambda service, **kw: calls.append((service, kw))
        app._apply_programme_ui_dropdowns("uld")
        self.assertNotIn(("input_boolean/turn_off", {"entity_id": SHORT}), calls)

    def test_the_end_of_the_cycle_switches_it_off(self):
        app = self.running_app(kort_helper="on")
        calls = []
        app.call_service = lambda service, **kw: calls.append((service, kw))
        app._transition_to_off("test", force=True)
        self.assertIn(("input_boolean/turn_off", {"entity_id": SHORT}), calls)

    def test_no_helper_configured_means_no_calls(self):
        app = self.running_app(option_short_entity=None)
        calls = []
        app.call_service = lambda service, **kw: calls.append((service, kw))
        app._transition_to_off("test", force=True)
        app._apply_programme_ui_dropdowns("uld")
        self.assertFalse([c for c in calls if c[1].get("entity_id") == SHORT])
        self.assertFalse(app._short_on("bomuld"))


class AFinishedKortWash(MonitorTestCase):
    """The record of a wash is saved when it finishes. Kort switched on afterwards, while the wash waits to be emptied,
    marks that record, and switching it off again undoes it."""

    def app_with_saved_wash(self, confirmed=True, minutes=100.0, short=False):
        feedback_file = os.path.join(self.tmp, "washer_feedback.json")
        app = self.running_app(kort_helper="on" if short else "off", confirmed=confirmed, cycles=(),
                               feedback_file=feedback_file)
        app.now = START + timedelta(minutes=minutes)
        options = app._get_selected_options()
        classification = app._classify_cycle_completion(
            run_minutes=minutes, energy_kwh=1.0, heating_bursts=2, max_power_w=2000.0, predicted="bomuld",
            predicted_temperature="60°C", confirmed="bomuld", confirmed_temperature="60°C",
            transition_path="spin_end", selected_options=options)
        app._save_cycle_feedback(
            predicted="bomuld", predicted_temperature="60°C", confirmed="bomuld", confirmed_temperature="60°C",
            duration_min=minutes, energy_kwh=1.0, heating_bursts=2, max_power_w=2000.0, user_confirmed=confirmed,
            completion_class=classification["completion_class"], valid_for_learning=classification["valid_for_learning"],
            validation_flags=classification["validation_flags"], selected_options=options)
        app.states[STATE] = "Unemptied"
        app.state = "Unemptied"
        return app, feedback_file

    def stored(self, feedback_file):
        with open(feedback_file) as f:
            return json.load(f)["cycles"][-1]

    def test_a_kort_wash_is_saved_as_kort_and_stays_out_of_the_plain_averages(self):
        app, feedback_file = self.app_with_saved_wash(short=True)
        rec = self.stored(feedback_file)
        self.assertEqual(rec["selected_options"]["short"], "on")
        self.assertFalse(rec["valid_for_learning"])
        self.assertIn("short_selected", rec["validation_flags"])
        self.assertNotIn("bomuld|60°C", app._learned_durations)
        self.assertEqual(app._eta_history, [])
        self.assertEqual([d for d, _ in app._eta_history_short["bomuld|60°C"]], [100.0])

    def test_a_plain_wash_still_learns_normally(self):
        app, feedback_file = self.app_with_saved_wash(minutes=150.0, short=False)
        self.assertEqual(app._learned_durations["bomuld|60°C"]["n"], 1)
        self.assertEqual([d for d, _ in app._eta_history], [150.0])
        self.assertEqual(app._eta_history_short, {})

    def test_switching_kort_on_afterwards_marks_the_record_and_takes_it_out_of_the_plain_data(self):
        app, feedback_file = self.app_with_saved_wash(short=False)
        self.assertEqual(app._learned_durations["bomuld|60°C"]["n"], 1)
        app.states[SHORT] = "on"
        app._on_short_option_changed(SHORT, "state", "off", "on", {})
        rec = self.stored(feedback_file)
        self.assertEqual(rec["selected_options"]["short"], "on")
        self.assertFalse(rec["valid_for_learning"])
        self.assertIn("short_selected", rec["validation_flags"])
        self.assertNotIn("bomuld|60°C", app._learned_durations)
        self.assertEqual(app._eta_history, [])
        self.assertEqual(len(app._eta_history_short["bomuld|60°C"]), 1)

    def test_switching_it_off_again_gives_the_wash_back_to_the_plain_data(self):
        app, feedback_file = self.app_with_saved_wash(short=True)
        app.states[SHORT] = "off"
        app._on_short_option_changed(SHORT, "state", "on", "off", {})
        rec = self.stored(feedback_file)
        self.assertEqual(rec["selected_options"]["short"], "off")
        self.assertTrue(rec["valid_for_learning"])
        self.assertEqual(app._learned_durations["bomuld|60°C"]["n"], 1)
        self.assertEqual(app._eta_history_short, {})

    def test_the_same_value_again_changes_nothing(self):
        app, feedback_file = self.app_with_saved_wash(short=True)
        before = raw(feedback_file)
        app._on_short_option_changed(SHORT, "state", "off", "on", {})
        self.assertEqual(raw(feedback_file), before)

    def test_only_an_unemptied_wash_is_marked(self):
        for state in ("Running", "Off", "Emptied", "Paused"):
            app, feedback_file = self.app_with_saved_wash(short=False)
            app.states[STATE] = state
            before = raw(feedback_file)
            app.states[SHORT] = "on"
            app._on_short_option_changed(SHORT, "state", "off", "on", {})
            self.assertEqual(raw(feedback_file), before, state)

    def test_the_end_of_the_cycle_does_not_unmark_the_wash(self):
        app, feedback_file = self.app_with_saved_wash(short=True)
        app.states[STATE] = "Off"
        app.states[SHORT] = "off"
        app._on_short_option_changed(SHORT, "state", "on", "off", {})
        self.assertEqual(self.stored(feedback_file)["selected_options"]["short"], "on")

    def test_a_wash_whose_programme_nobody_confirmed_is_left_alone(self):
        app, feedback_file = self.app_with_saved_wash(confirmed=False, short=False)
        before = raw(feedback_file)
        app.states[SHORT] = "on"
        app._on_short_option_changed(SHORT, "state", "off", "on", {})
        self.assertEqual(raw(feedback_file), before)

    def test_a_record_older_than_twelve_hours_is_left_alone(self):
        app, feedback_file = self.app_with_saved_wash(short=False)
        app.now = app.now + timedelta(hours=13)
        before = raw(feedback_file)
        app.states[SHORT] = "on"
        app._on_short_option_changed(SHORT, "state", "off", "on", {})
        self.assertEqual(raw(feedback_file), before)

    def test_a_missing_feedback_file_is_not_an_error(self):
        app = self.running_app(feedback_file=os.path.join(self.tmp, "none.json"))
        app.states[STATE] = "Unemptied"
        app.states[SHORT] = "on"
        app._on_short_option_changed(SHORT, "state", "off", "on", {})
        self.assertTrue([m for m in trs.log_messages(app, "WARNING") if "Kort" in m])


if __name__ == "__main__":
    unittest.main()
