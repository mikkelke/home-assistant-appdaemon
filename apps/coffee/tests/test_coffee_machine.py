# tests/test_coffee_machine.py - CoffeeMachine end to end on a fake AppDaemon (coffee_fake.py): what it
# publishes, a replayed brew (real plug readings, the integration's event order) into the ledger and a
# curve file, the start, the link, who is told, beans, the advisor with a stubbed AI task, restarts, and
# guards against shadowing AppDaemon.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import ast
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coffee_fake as fake  # noqa: E402
from coffee_fake import KRISTINE, MIKKEL, T0, TABLET  # noqa: E402

import coffee_machine as cm  # noqa: E402
import coffee_plan as cp  # noqa: E402
import coffee_publish as pub  # noqa: E402
import run_ledger  # noqa: E402

POWER = json.loads((Path(__file__).resolve().parent / "fixtures" / "plug_power.json").read_text())
BREW_ID = f"c{int(T0)}"
COFFEE = [f"sensor.{key}" for key in cm.ENTITIES]

# AppDaemon 4.5.13's ADAPI, Hass and ADBase members and attributes (read from the box's install): an app
# attribute with one of these names breaks AppDaemon, as `_log` once did (ActorAttribution, 27.07).
APPDAEMON_NAMES = frozenset("""
AD _check_entity _check_service _config_model _create_helper _domain_service_call _entity_service_call
_get_namespace _log _logging _namespace _notify_mobile_app _plugin _sub_stack _template_command add_entity
add_namespace android_tts anyone_home app_config app_dir area_devices area_entities area_id area_name areas args
backup_full backup_partial call_service callback_counter cancel_listen_event cancel_listen_log
cancel_listen_state cancel_sequence cancel_timer check_for_entity config config_dir config_model
constrain_input_boolean constrain_input_select constrain_person constrain_presence constraints convert_utc
create_task dash_navigate dashboard_dir date datetime depends_on_module deregister_constraint
deregister_endpoint deregister_route deregister_service device_attr device_entities device_id dialogflow_v
entities entity_exists err error everyone_home fire_event format_alexa_response format_dialogflow_response
friendly_name get_ad_api get_ad_version get_alexa_error get_alexa_intent get_alexa_slot_value get_app
get_app_pin get_app_python_dependencies get_calendar_events get_callback_entries get_dialogflow_intent
get_dialogflow_slot_value get_entity get_entity_callbacks get_error_log get_history get_logbook get_main_log
get_namespace get_now get_now_ts get_pin_thread get_plugin_api get_plugin_config get_scheduler_entries
get_service_info get_state get_thread_info get_timezone get_tracker_details get_tracker_state get_trackers
get_tz_offset get_user_log global_vars info_listen_event info_listen_state info_timer integration_entities
is_device_attr label_areas label_devices label_entities label_id label_name labels last_pressed
list_namespaces list_services listen_event listen_log listen_notification_action listen_state lock log logger
media_mute media_pause media_play media_play_pause media_seek media_set_volume name namespace namespace_exists
noone_home notify notify_android notify_ios now_is_between parse_datetime parse_time parse_utc_string
persistent_notification ping plugin_config press_button process_conversation register_constraint
register_endpoint register_route register_service reload_apps reload_conversation remove_entity
remove_namespace render_template reset_timer restart_app restore_full restore_partial run_at run_at_sunrise
run_at_sunset run_daily run_every run_hourly run_in run_in_executor run_in_thread run_minutely run_once
run_script run_sequence save_namespace select_first select_last select_next select_option select_previous
set_app_pin set_error_level set_log_level set_namespace set_options set_pin_thread set_production_mode
set_state set_textvalue set_value sleep split_device_list split_entity start_app stop_app submit_to_executor
sun_down sun_up sunrise sunset time time_since_last_press timer_running toggle turn_off turn_on user_logs
""".split())


def coffee_state(app):
    return fake.last(app, "sensor.coffee_state")


def states_seen(app):
    seen = []
    for _, entity, state, _ in app.history:
        if entity == "sensor.coffee_state" and (not seen or seen[-1] != state):
            seen.append(state)
    return seen


def calls(app, service):
    return [data for name, data in app.calls if name == service]


def brew(app, ours=True, user=TABLET, power=True, brew_id=BREW_ID, **kw):
    tape = fake.synthetic_brew(brew_id, POWER["brew_2026_10_17"] if power else None, ours=ours, **kw)
    if ours:
        tape = [(0, ("op", "start", {"variant": "usual"}, user))] + [s for s in tape if s[1][0] != "op"]
    fake.replay(app, tape)


def seed_ledger(app, records):
    path = app.dir / "coffee_feedback.json"
    data = json.loads(path.read_text())
    data["cycles"] += records
    path.write_text(json.dumps(data))
    app._load_ledger(force=True)


def rated_usual(app, i, taste):
    usual = cp.normalize(fake.USUAL)
    at = fake.iso(T0 - (10 - i) * 86400)
    return {"id": f"c{int(T0) - (10 - i) * 86400}", "ts": at, "started_at": at, "duration_min": 4.0,
            "completion_class": "complete", "outcome": "complete", "source": "tablet", "variant": "usual",
            "recipe": {"id": usual["id"], "name": usual["name"], "key": cp.recipe_key(usual), "grind": 46},
            "bean": app._beans.bean(), "coverage": 0.98, "total_s": 205, "cup_g": 215.0, "drawdown_s": 60,
            "rating": taste}


class PublishTests(unittest.TestCase):
    def test_init_publishes_the_seven_entities_v1_string_leaves(self):
        app = fake.make_app()
        self.assertEqual(sorted(e for e in app.published if e.startswith("sensor.coffee_")), sorted(COFFEE))
        for entity in COFFEE:
            state, attrs, replace = app.published[entity]
            self.assertTrue(replace, entity)
            self.assertIsInstance(state, str, entity)
            self.assertEqual(attrs["v"], "1", entity)
            for key, value in attrs.items():
                self.assertTrue(pub.leaves_ok(value), (entity, key, value))
        self.assertEqual(coffee_state(app)[0], "idle")
        self.assertEqual(fake.last(app, "sensor.coffee_recipes")[0], "4 Pour Recipe")
        self.assertEqual(fake.last(app, "sensor.coffee_baseline")[0], "building")
        self.assertEqual(fake.last(app, "sensor.coffee_beans")[0], "unknown")
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "no_agent")
        self.assertEqual(fake.last(app, "sensor.coffee_live")[0], "idle")

    def test_plugin_started_republishes_all_seven(self):
        app = fake.make_app()
        before = len(app.history)
        fake.fire(app, "plugin_started")
        self.assertEqual({e for _, e, _, _ in app.history[before:]}, set(COFFEE))

    def test_the_ledger_file_exists_from_the_start(self):
        app = fake.make_app(recipes=())
        self.assertEqual(json.loads((app.dir / "coffee_feedback.json").read_text()), {"version": 1, "cycles": []})
        self.assertTrue((app.dir / "media" / "brews").is_dir())

    def test_the_machine_class_and_asleep(self):
        app = fake.make_app()
        fake.set_entity(app, "sensor.pour_over_plug_power", "0.9")
        self.assertEqual((coffee_state(app)[0], coffee_state(app)[1]["machine"]), ("asleep", "asleep"))
        fake.set_entity(app, "switch.pour_over_plug", "off")
        self.assertEqual(coffee_state(app)[1]["machine"], "off")


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = fake.make_app()
        brew(cls.app)
        fake.advance(cls.app, 30)

    def test_the_record(self):
        record = fake.ledger(self.app)[-1]
        self.assertEqual((record["id"], record["outcome"], record["source"], record["variant"], record["recipe_match"]),
                         (BREW_ID, "complete", "tablet", "usual", "sent"))
        self.assertEqual(record["first_bloom_at"], fake.iso(T0 + 28).replace(".000000", ""))
        self.assertEqual(record["enjoy_at"], cm.cf.iso(T0 + 235.1))
        self.assertEqual((record["total_s"], record["drawdown_s"]), (207, 62))
        self.assertEqual((record["cup_g"], record["water_ml"]), (217.4, 255.0))
        self.assertGreaterEqual(record["coverage"], 0.95)
        self.assertEqual((record["run_id"], record["dose_g"], record["dose_basis"]), ("r1", 15.0, "sent"))
        self.assertEqual(record["completion_class"], "complete")
        self.assertIn("picked_up_at", record)
        self.assertEqual((record["who"], record["notify"], record["push"]), ("none", "none", "none"))
        self.assertEqual(record["recipe"]["key"], cp.recipe_key(cp.normalize(fake.USUAL)))

    def test_one_run_for_the_maintenance_rows(self):
        self.assertEqual(len(run_ledger.ledger(fake.ledger(self.app)).runs), 1)

    def test_the_curve_file_has_one_value_a_second(self):
        data = json.loads((self.app.dir / "media" / "brews" / f"{BREW_ID}.json").read_text())
        self.assertEqual((data["id"], data["step_s"], data["end_s"]), (BREW_ID, 1, 207))
        self.assertEqual(len(data["cup"]), 208)
        self.assertEqual(len(data["water"]), 208)
        self.assertEqual(data["water"][-1], 2550)
        self.assertEqual([idx for _, idx in data["pours"]], [0, 1, 2, 3])
        self.assertEqual(data["t0"], T0 + 28)

    def test_the_states_it_walked_through(self):
        self.assertEqual(states_seen(self.app), ["idle", "starting", "grinding", "brewing", "ready", "picked_up",
                                                 "idle"])

    def test_live_once_a_second_and_state_only_at_transitions(self):
        live = [t for t, e, s, _ in self.app.history if e == "sensor.coffee_live" and T0 + 28 <= t <= T0 + 235]
        self.assertGreaterEqual(len(live), 200)
        state = [t for t, e, s, _ in self.app.history if e == "sensor.coffee_state" and T0 <= t <= T0 + 300]
        self.assertLess(len(state), 40)

    def test_coffee_live_ends_with_the_curve(self):
        state, attrs = fake.last(self.app, "sensor.coffee_live")
        self.assertEqual((state, attrs["brew_id"], attrs["end_s"]), ("ended", BREW_ID, "207"))
        self.assertEqual(len(attrs["cup"].split(",")), 208)

    def test_one_house_event_at_ready(self):
        reports = [d for e, d in self.app.fired if e == "house_events_report"]
        self.assertEqual(reports, [{"icon": "mdi:coffee", "text": "Coffee ready", "effect": "217 g"}])

    def test_recent_json(self):
        doc = json.loads((self.app.dir / "media" / "recent.json").read_text())
        self.assertEqual([b["id"] for b in doc["brews"]], [BREW_ID])
        self.assertEqual(doc["step_s"], 2)
        self.assertEqual(len(doc["brews"][0]["cup"]), 104)

    def test_the_brews_entity(self):
        state, attrs = fake.last(self.app, "sensor.coffee_brews")
        self.assertEqual(attrs["brews"][0]["id"], BREW_ID)
        self.assertEqual(attrs["brews"][0]["total_s"], "207")
        self.assertEqual(attrs["brews_60d"], "1")
        self.assertTrue(attrs["last_curve"])

    def test_the_link_went_on_for_the_brew_and_off_ten_minutes_later(self):
        app = fake.make_app()
        brew(app)
        self.assertEqual(calls(app, "switch/turn_on"), [{"entity_id": "switch.xbloom_studio_connect"}])
        picked_up = fake.ledger(app)[-1]["picked_up_at"]
        quiet_end = cm.cf.epoch(picked_up) + 600
        fake.advance(app, quiet_end - 5 - app.now)
        self.assertEqual(calls(app, "switch/turn_off"), [])
        fake.advance(app, 20)
        self.assertEqual(len(calls(app, "switch/turn_off")), 1)


class StartTests(unittest.TestCase):
    def test_starting_then_grinding(self):
        app = fake.make_app()
        fake.op(app, "start", {"variant": "usual"})
        self.assertEqual(coffee_state(app)[0], "starting")
        self.assertEqual(calls(app, "switch/turn_on"), [{"entity_id": "switch.xbloom_studio_connect"}])
        self.assertEqual(calls(app, "xbloom/start_brew"), [])
        fake.set_entity(app, "switch.xbloom_studio_connect", "on")
        fake.fire(app, "xbloom_connect_ready")
        sent = calls(app, "xbloom/start_brew")
        self.assertEqual(sent, [{"return_response": True, "recipe_name": "4 Pour Recipe",
                                 "context": {"coffee_brew_id": BREW_ID}}])
        fake.fire(app, "xbloom_brew_started", {"recipe_name": "4 Pour Recipe", "run_id": "r1",
                                               "context": {"coffee_brew_id": BREW_ID}})
        state, attrs = coffee_state(app)
        self.assertEqual((state, attrs["brew_id"], attrs["source"], attrs["variant"]),
                         ("grinding", BREW_ID, "tablet", "usual"))

    def test_a_second_start_while_starting_does_nothing(self):
        app = fake.make_app()
        fake.op(app, "start", {"variant": "usual"})
        fake.advance(app, 2)
        fake.op(app, "start", {"variant": "usual"}, user_id=MIKKEL)
        fake.fire(app, "xbloom_connect_ready")
        self.assertEqual(len(calls(app, "xbloom/start_brew")), 1)
        self.assertEqual(coffee_state(app)[1]["brew_id"], BREW_ID)

    def test_a_refusal_is_start_failed_with_the_machines_reason_then_idle(self):
        app = fake.make_app()
        fake.op(app, "start", {"variant": "usual"})
        fake.fire(app, "xbloom_connect_failed", {"reason": "machine_busy"})
        self.assertEqual(len(calls(app, "xbloom/start_brew")), 1)
        fake.fire(app, "xbloom_brew_failed", {"reason": "machine_not_found", "recipe_name": "4 Pour Recipe",
                                              "run_id": "r1", "context": {"coffee_brew_id": BREW_ID}})
        state, attrs = coffee_state(app)
        self.assertEqual((state, attrs["start_error"]), ("start_failed", "machine_not_found"))
        fake.advance(app, 70)
        self.assertEqual(coffee_state(app)[0], "idle")
        self.assertEqual(fake.ledger(app), [])

    def test_no_answer_after_45_s(self):
        app = fake.make_app()
        app.responses["xbloom/start_brew"] = fake.ok({"run_id": None, "duplicate": True})
        fake.op(app, "start", {"variant": "usual"})
        fake.advance(app, 16)
        self.assertEqual(len(calls(app, "xbloom/start_brew")), 1)
        state, attrs = coffee_state(app)
        self.assertEqual((state, attrs["start_error"]), ("start_failed", "machine_busy"))
        app2 = fake.make_app()
        app2.responses["xbloom/start_brew"] = {"success": False, "error": {"message": "x"}}
        fake.op(app2, "start", {"variant": "usual"})
        fake.advance(app2, 16)
        self.assertEqual(coffee_state(app2)[1]["start_error"], "no_answer")
        app3 = fake.make_app()
        fake.op(app3, "start", {"variant": "usual"})
        fake.advance(app3, 44)
        self.assertEqual(coffee_state(app3)[0], "starting")
        fake.advance(app3, 2)
        self.assertEqual(coffee_state(app3)[1]["start_error"], "no_answer")

    def test_the_start_goes_at_once_on_a_live_link(self):
        app = fake.make_app()
        fake.set_entity(app, "switch.xbloom_studio_connect", "on")
        fake.set_entity(app, "sensor.xbloom_studio_status_updated", fake.iso(app.now))
        fake.op(app, "start", {"variant": "usual"})
        self.assertEqual(len(calls(app, "xbloom/start_brew")), 1)
        self.assertEqual(calls(app, "switch/turn_on"), [])

    def test_without_the_recipe_in_the_library(self):
        app = fake.make_app(recipes=())
        fake.op(app, "start", {"variant": "usual"})
        state, attrs = coffee_state(app)
        self.assertEqual((state, attrs["start_error"]), ("start_failed", "recipe_not_found"))
        self.assertEqual(calls(app, "xbloom/start_brew"), [])

    def test_tweak_once_and_another_recipe(self):
        app = fake.make_app()
        fake.set_entity(app, "switch.xbloom_studio_connect", "on")
        fake.set_entity(app, "sensor.xbloom_studio_status_updated", fake.iso(app.now))
        fake.op(app, "start", {"variant": "tweak", "dose": "16", "grind_size": 44}, user_id=MIKKEL)
        self.assertEqual(calls(app, "xbloom/start_brew")[-1]["grind_size"], 44)
        self.assertEqual(calls(app, "xbloom/start_brew")[-1]["dose"], 16.0)
        self.assertEqual(coffee_state(app)[1]["variant"], "tweak")
        self.assertEqual(coffee_state(app)[1]["source"], "phone")
        app = fake.make_app()
        fake.set_entity(app, "switch.xbloom_studio_connect", "on")
        fake.set_entity(app, "sensor.xbloom_studio_status_updated", fake.iso(app.now))
        fake.op(app, "start", {"variant": "other", "recipe_id": "7654321"}, as_text=False)
        self.assertEqual(calls(app, "xbloom/start_brew")[-1]["recipe_name"], "Iced")

    def test_invalid_ops_are_ignored(self):
        app = fake.make_app()
        for name, data in (("start", {"variant": "tweak", "dose": 99}), ("start", {"variant": "espresso"}),
                           ("brew", {}), ("notify", {"person": "bob"}), ("rate", {"brew_id": "c1", "taste": "meh"})):
            fake.op(app, name, data)
        fake.fire(app, "coffee_command", {"op": "start", "data": "{not json"}, TABLET)
        self.assertEqual(coffee_state(app)[0], "idle")
        self.assertEqual(calls(app, "xbloom/start_brew"), [])
        self.assertTrue(any(level == "WARNING" for level, _ in app.logs))

    def test_stop_pause_and_resume_call_the_integration(self):
        app = fake.make_app()
        fake.set_entity(app, "sensor.xbloom_studio_brew_status", "grinding")
        fake.op(app, "pause")
        fake.set_entity(app, "binary_sensor.xbloom_studio_brew_paused", "on")
        fake.op(app, "resume")
        fake.set_entity(app, "binary_sensor.xbloom_studio_brew_paused", "off")
        fake.op(app, "stop")
        self.assertEqual([n for n, _ in app.calls if n.startswith("xbloom/brew") or n == "xbloom/stop_brew"],
                         ["xbloom/brew_pause", "xbloom/brew_resume", "xbloom/stop_brew"])
        fake.set_entity(app, "sensor.xbloom_studio_brew_status", "idle")
        self.assertEqual(coffee_state(app)[1]["stop_reason"], "home_assistant")


class RestoreTests(unittest.TestCase):
    def test_values_restored_by_an_ha_restart_are_no_news(self):
        app = fake.make_app()
        fake.set_entity(app, "sensor.xbloom_studio_brew_status", "grinding")
        fake.advance(app, 30)
        for entity in ("event.xbloom_studio_brew_event", "sensor.xbloom_studio_scale_weight",
                       "sensor.xbloom_studio_status_updated"):
            fake.set_entity(app, entity, "unavailable")
        fake.set_entity(app, "event.xbloom_studio_brew_event", fake.iso(app.now - 3600),
                        {"event_type": "brew_done", "recipe_name": ""})
        fake.set_entity(app, "sensor.xbloom_studio_scale_weight", "216.0")
        fake.set_entity(app, "sensor.xbloom_studio_status_updated", fake.iso(app.now - 3600))
        state, attrs = coffee_state(app)
        self.assertEqual((state, attrs["link"]), ("grinding", "not_connected"))
        self.assertIsNone(app._tracker.cup)


class ConnectTests(unittest.TestCase):
    def test_a_rise_through_10_w_turns_connect_on_at_once(self):
        app = fake.make_app()
        fake.set_entity(app, "sensor.pour_over_plug_power", "38.0")
        self.assertEqual(len(calls(app, "switch/turn_on")), 1)

    def test_one_trigger_per_brew_not_one_per_heater_pulse(self):
        app = fake.make_app()
        app.switch_follows = False
        rows = POWER["brew_2026_10_17"]
        for ts, value in rows:
            fake.advance(app, max(0.0, ts - rows[0][0] + T0 - app.now))
            fake.set_entity(app, "sensor.pour_over_plug_power", value)
        self.assertEqual(len(calls(app, "switch/turn_on")), 1)

    def test_waking_up_or_an_ha_restart_is_no_trigger(self):
        app = fake.make_app()
        fake.set_entity(app, "sensor.pour_over_plug_power", "0.9")
        fake.set_entity(app, "sensor.pour_over_plug_power", "2.3")
        fake.set_entity(app, "sensor.pour_over_plug_power", "unavailable")
        fake.set_entity(app, "sensor.pour_over_plug_power", "30.0")
        self.assertEqual(calls(app, "switch/turn_on"), [])

    def test_the_connect_op(self):
        app = fake.make_app()
        fake.op(app, "connect")
        self.assertEqual(len(calls(app, "switch/turn_on")), 1)

    def test_no_reconnect_loop_while_the_machine_idles_awake(self):
        app = fake.make_app()
        fake.set_entity(app, "sensor.pour_over_plug_power", "38.0")
        fake.set_entity(app, "switch.xbloom_studio_connect", "on")
        fake.set_entity(app, "sensor.pour_over_plug_power", "2.3")
        for _ in range(12):
            fake.advance(app, 600)
            if calls(app, "switch/turn_off") and app.states["switch.xbloom_studio_connect"]["state"] == "on":
                fake.set_entity(app, "switch.xbloom_studio_connect", "off")
        self.assertEqual((len(calls(app, "switch/turn_on")), len(calls(app, "switch/turn_off"))), (1, 1))

    def test_retries_while_the_machine_works(self):
        app = fake.make_app()
        fake.set_entity(app, "sensor.pour_over_plug_power", "38.0")
        for _ in range(3):
            fake.fire(app, "xbloom_connect_failed", {"reason": "machine_busy"})
            fake.set_entity(app, "switch.xbloom_studio_connect", "off")
            fake.advance(app, 25)
        fake.advance(app, 60)
        self.assertEqual(len(calls(app, "switch/turn_on")), 3)

    def test_a_person_switching_it_off_stops_automatic_connects(self):
        app = fake.make_app()
        fake.set_entity(app, "switch.xbloom_studio_connect", "on")
        fake.set_entity(app, "switch.xbloom_studio_connect", "off", user_id=MIKKEL)
        fake.set_entity(app, "sensor.pour_over_plug_power", "38.0")
        self.assertEqual(calls(app, "switch/turn_on"), [])
        fake.op(app, "connect")
        self.assertEqual(len(calls(app, "switch/turn_on")), 1)


class WhoTests(unittest.TestCase):
    def test_a_start_from_a_persons_phone_pushes_to_them_with_rating_buttons(self):
        app = fake.make_app()
        brew(app, user=MIKKEL)
        sent = app.notifier.sent
        self.assertEqual(len(sent), 1)
        push = sent[0]
        self.assertEqual((push["title"], push["message"], push["target"]),
                         ("Coffee's ready", "217 g, 3:27, the usual", ["mikkel"]))
        self.assertEqual(push["link"], "/dashboard-rooftop/home#room=kitchen")
        self.assertEqual(push["data"]["data"]["tag"], f"coffee_{BREW_ID}")
        self.assertEqual([a["action"] for a in push["data"]["data"]["actions"]],
                         [f"COFFEE_RATE|{BREW_ID}|{t}" for t in ("sour", "balanced", "bitter")])
        record = fake.ledger(app)[-1]
        self.assertEqual((record["who"], record["who_method"], record["push"]), ("mikkel", "dashboard_action", "sent"))
        fake.fire(app, "mobile_app_notification_action", {"action": f"COFFEE_RATE|{BREW_ID}|sour"}, MIKKEL)
        record = fake.ledger(app)[-1]
        self.assertEqual((record["rating"], record["rated_by"]), ("sour", "mikkel"))
        self.assertEqual(fake.last(app, "sensor.coffee_brews")[1]["brews"][0]["rating"], "sour")

    def test_push_failed_when_nothing_was_delivered(self):
        app = fake.make_app()
        app.notifier.deliver = 0
        brew(app, user=MIKKEL)
        self.assertEqual(fake.ledger(app)[-1]["push"], "failed")

    def test_a_tablet_start_with_nobody_resolved_pushes_to_nobody(self):
        app = fake.make_app()
        brew(app)
        self.assertEqual(app.notifier.sent, [])
        self.assertEqual(fake.ledger(app)[-1]["push"], "none")

    def test_sole_occupancy_names_a_machine_start(self):
        app = fake.make_app()
        app.attribution.sole = "kristine"
        brew(app, ours=False)
        record = fake.ledger(app)[-1]
        self.assertEqual((record["source"], record["who"], record["who_method"], record["notify"]),
                         ("machine", "kristine", "sole_occupancy", "kristine"))
        self.assertEqual(app.notifier.sent[0]["target"], ["kristine"])
        app = fake.make_app(sole_occupancy=False)
        app.attribution.sole = "kristine"
        brew(app, ours=False)
        self.assertEqual(fake.ledger(app)[-1]["who"], "none")

    def test_a_chip_pick_while_idle_goes_to_the_next_brew(self):
        app = fake.make_app()
        fake.op(app, "notify", {"person": "claudia"})
        self.assertEqual(coffee_state(app)[1]["notify"], "claudia")
        brew(app)
        record = fake.ledger(app)[-1]
        self.assertEqual((record["who"], record["who_method"], record["notify"]), ("claudia", "chip", "claudia"))
        self.assertEqual(app.notifier.sent[0]["target"], ["claudia"])

    def test_bermuda_names_mikkel_when_his_phone_came_to_the_machine(self):
        app = fake.make_app()
        distance, area = fake.CONFIG["bermuda_distance"], fake.CONFIG["bermuda_area"]
        fake.set_entity(app, area, "Kitchen")
        fake.advance(app, 60)
        fake.set_entity(app, distance, "1.4")
        brew(app, ours=False)
        record = fake.ledger(app)[-1]
        self.assertEqual((record["who"], record["who_method"]), ("mikkel", "bermuda"))
        self.assertEqual(record["who_evidence"]["closest_m"], "1.4")

    def test_no_push_for_a_plug_only_brew(self):
        app = fake.make_app()
        app.attribution.sole = "kristine"
        start = app.now
        for ts, value in POWER["brew_2026_10_17"]:
            fake.advance(app, max(0.0, ts - POWER["brew_2026_10_17"][0][0] + start - app.now))
            fake.set_entity(app, "sensor.pour_over_plug_power", value)
        fake.advance(app, 120)
        record = fake.ledger(app)[-1]
        self.assertEqual((record["source"], record["outcome"], record["who"], record["push"]),
                         ("power", "unobserved", "kristine", "none"))
        self.assertEqual(app.notifier.sent, [])
        self.assertEqual(record["dose_basis"], "usual")

    def test_no_water_mid_brew_pushes_once(self):
        app = fake.make_app()
        tape = fake.synthetic_brew(BREW_ID, None)
        tape = [(0, ("op", "start", {"variant": "usual"}, MIKKEL))] + [s for s in tape if s[1][0] != "op"]
        tape += [(90, ("state", "sensor.xbloom_studio_machine_status", "no_water")),
                 (91, ("state", "sensor.xbloom_studio_machine_status", "ok")),
                 (92, ("state", "sensor.xbloom_studio_machine_status", "no_water"))]
        fake.replay(app, tape)
        faults = [p for p in app.notifier.sent if p["data"]["data"]["tag"] == "coffee_fault"]
        self.assertEqual(len(faults), 1)
        self.assertEqual(fake.ledger(app)[-1]["faults"], ["no_water"])

    def test_the_tablet_wakes_for_a_machine_start_only(self):
        app = fake.make_app()
        brew(app, ours=False)
        wake = [d for n, d in app.calls if n == "notify/mobile_app_oppopad4pro"]
        brew_id = fake.ledger(app)[-1]["id"]
        self.assertEqual(wake, [{"message": "command_screen_on"},
                                {"message": "command_webview",
                                 "data": {"command": f"/dashboard-rooftop/home#coffee&takeover={brew_id}"}}])
        app = fake.make_app()
        brew(app)
        self.assertEqual([d for n, d in app.calls if n == "notify/mobile_app_oppopad4pro"], [])
        app = fake.make_app()
        fake.set_entity(app, "binary_sensor.oppopad4pro_interactive", "on")
        fake.set_entity(app, "binary_sensor.oppopad4pro_interactive", "off")
        brew(app, ours=False)
        self.assertEqual([d for n, d in app.calls if n == "notify/mobile_app_oppopad4pro"], [])


class BeansTests(unittest.TestCase):
    def test_add_brew_switch_and_undo(self):
        app = fake.make_app()
        fake.op(app, "beans_add", {"grams": 250, "new_name": "Kenya Kiambu"})
        state, attrs = fake.last(app, "sensor.coffee_beans")
        self.assertEqual((state, attrs["bean_name"], attrs["brews_left"], attrs["dose_g"]), ("250", "Kenya Kiambu",
                                                                                            "16", "15"))
        brew(app)
        self.assertEqual(fake.last(app, "sensor.coffee_beans")[0], "235")
        record = fake.ledger(app)[-1]
        self.assertEqual((record["bean"], record["debited_g"]), ({"id": "b1", "name": "Kenya Kiambu"}, 15.0))
        fake.advance(app, 600)
        fake.op(app, "beans_add", {"grams": "250", "new_name": "Ethiopia Guji"})
        state, attrs = fake.last(app, "sensor.coffee_beans")
        self.assertEqual((state, attrs["bean_id"]), ("485", "b2"))
        fake.op(app, "beans_undo", {"op_id": attrs["last_op_id"]})
        state, attrs = fake.last(app, "sensor.coffee_beans")
        self.assertEqual((state, attrs["bean_id"]), ("235", "b1"))
        self.assertNotIn("last_op_id", attrs)

    def test_a_stopped_brew_that_ground_debits_its_dose(self):
        app = fake.make_app()
        fake.op(app, "beans_add", {"grams": 100, "bean_id": None, "new_name": "Kenya"})
        tape = [s for s in fake.synthetic_brew(BREW_ID, None) if s[0] <= 60]
        tape.append((61, ("event", "xbloom_brew_stopped", {"by": "machine", "ground": True, "run_id": "r1",
                                                           "context": {"coffee_brew_id": BREW_ID}})))
        fake.replay(app, tape)
        record = fake.ledger(app)[-1]
        self.assertEqual((record["outcome"], record["stop_reason"], record["completion_class"]),
                         ("stopped", "machine", "interrupted"))
        self.assertEqual(fake.last(app, "sensor.coffee_beans")[0], "85")
        self.assertEqual(len(run_ledger.ledger(fake.ledger(app)).runs), 0)

    def test_a_stop_before_the_machine_took_it_leaves_no_record_and_no_debit(self):
        app = fake.make_app()
        fake.op(app, "beans_add", {"grams": 100, "new_name": "Kenya"})
        fake.op(app, "start", {"variant": "usual"})
        fake.op(app, "stop")
        self.assertEqual(fake.ledger(app), [])
        self.assertEqual(fake.last(app, "sensor.coffee_beans")[0], "100")


class AdvisorTests(unittest.TestCase):
    def test_no_agent_by_default_and_no_ask(self):
        app = fake.make_app()
        fake.op(app, "beans_add", {"grams": 250, "new_name": "Kenya"})
        seed_ledger(app, [rated_usual(app, i, "sour") for i in range(4)])
        brew(app)
        fake.op(app, "rate", {"brew_id": BREW_ID, "taste": "sour"}, user_id=MIKKEL)
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "no_agent")
        self.assertEqual(calls(app, "ai_task/generate_data"), [])
        fake.op(app, "trial_accept", {"advice_id": "a1"})
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "no_agent")

    def flow(self):
        app = fake.make_app(advisor_entity="ai_task.stub")
        app.responses["ai_task/generate_data"] = fake.ok({"conversation_id": "x", "data": {
            "action": "try", "to": 44, "confidence": "medium", "why": "Three of your four cups were sour."}})
        fake.op(app, "beans_add", {"grams": 250, "new_name": "Kenya"})
        seed_ledger(app, [rated_usual(app, i, "sour") for i in range(3)] + [rated_usual(app, 3, "balanced")])
        brew(app)
        self.assertEqual(calls(app, "ai_task/generate_data"), [])
        fake.op(app, "rate", {"brew_id": BREW_ID, "taste": "sour"}, user_id=MIKKEL)
        asked = calls(app, "ai_task/generate_data")
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["entity_id"], "ai_task.stub")
        self.assertEqual(set(asked[0]["structure"]), {"action", "to", "confidence", "why"})
        state, attrs = fake.last(app, "sensor.coffee_advice")
        self.assertEqual((state, attrs["from"], attrs["to"], attrs["confidence"]), ("suggested", "46", "44", "medium"))
        return app, attrs["advice_id"]

    def test_the_whole_trial_and_only_keep_changes_the_recipe(self):
        app, advice_id = self.flow()
        fake.op(app, "trial_accept", {"advice_id": advice_id})
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "trial_queued")
        app.now += 3600
        brew(app, brew_id=f"c{int(app.now)}")
        sent = calls(app, "xbloom/start_brew")[-1]
        self.assertEqual(sent["grind_size"], 44)
        record = fake.ledger(app)[-1]
        self.assertEqual((record["variant"], record["trial"], record["recipe"]["grind"]), ("trial", "grind_size:46>44",
                                                                                           44))
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "trial_brewed")
        self.assertEqual(calls(app, "xbloom/update_recipe"), [])
        app.responses["xbloom/get_recipe"] = fake.ok({"recipe": dict(fake.USUAL)})
        app.responses["xbloom/update_recipe"] = fake.ok({"ok": True, "name": "4 Pour Recipe"})
        fake.op(app, "trial_keep", {"advice_id": advice_id})
        updates = calls(app, "xbloom/update_recipe")
        self.assertEqual(len(updates), 1)
        saved = json.loads(updates[0]["recipe_json"])
        self.assertEqual((saved["grind_size"], saved["grinder_size"], saved["id"]), (44, 44, "81696"))
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "none")

    def test_revert_and_dismiss_never_touch_the_recipe(self):
        app, advice_id = self.flow()
        fake.op(app, "trial_accept", {"advice_id": advice_id})
        app.now += 3600
        brew(app, brew_id=f"c{int(app.now)}")
        fake.op(app, "trial_revert", {"advice_id": advice_id})
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "none")
        app, advice_id = self.flow()
        fake.op(app, "advice_dismiss", {"advice_id": advice_id})
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "dismissed")
        self.assertEqual(calls(app, "xbloom/update_recipe"), [])

    def test_a_base_start_keeps_the_trial_queued(self):
        app, advice_id = self.flow()
        fake.op(app, "trial_accept", {"advice_id": advice_id})
        app.now += 3600
        tape = fake.synthetic_brew(f"c{int(app.now)}", None)
        tape = [(0, ("op", "start", {"variant": "base"}, TABLET))] + [s for s in tape if s[1][0] != "op"]
        fake.replay(app, tape)
        self.assertNotIn("grind_size", calls(app, "xbloom/start_brew")[-1])
        self.assertEqual(fake.ledger(app)[-1]["variant"], "base")
        self.assertEqual(fake.last(app, "sensor.coffee_advice")[0], "trial_queued")

    def test_a_reply_asks_again_and_a_failure_is_an_error(self):
        app, advice_id = self.flow()
        app.responses["ai_task/generate_data"] = {"success": False, "error": {"message": "timeout"}}
        fake.op(app, "advisor_reply", {"advice_id": advice_id, "text": "It tasted bitter, not sour."}, user_id=MIKKEL)
        self.assertEqual(len(calls(app, "ai_task/generate_data")), 2)
        state, attrs = fake.last(app, "sensor.coffee_advice")
        self.assertEqual((state, attrs["error"]), ("error", "timeout"))
        self.assertEqual([e["role"] for e in attrs["thread"]], ["advisor", "person"])
        self.assertEqual(attrs["thread"][1]["who"], "mikkel")


class RestartTests(unittest.TestCase):
    def half(self):
        app = fake.make_app()
        tape = fake.synthetic_brew(BREW_ID, None)
        fake.replay(app, [s for s in tape if s[0] <= 100])
        fake.advance(app, T0 + 100 - app.now)
        self.assertEqual(coffee_state(app)[0], "brewing")
        app._save_state()
        return app, [(t, s) for t, s in tape if t > 110]

    def test_a_brew_in_flight_resumes_after_a_quick_restart(self):
        app, rest = self.half()
        again = fake.make_app(state_dir=app.dir, now=T0 + 110, states=app.states)
        self.assertEqual(coffee_state(again)[0], "brewing")
        fake.replay(again, rest, start=T0)
        record = fake.ledger(again)[-1]
        self.assertEqual((record["outcome"], record["total_s"]), ("complete", 207))
        self.assertLess(record["coverage"], 1.0)
        data = json.loads((again.dir / "media" / "brews" / f"{BREW_ID}.json").read_text())
        self.assertIn(None, data["cup"])

    def test_a_brew_too_old_to_resume_is_closed_as_abandoned(self):
        app, _ = self.half()
        states = dict(app.states)
        again = fake.make_app(state_dir=app.dir, now=app.now + 1500, states=states)
        record = fake.ledger(again)[-1]
        self.assertEqual((record["outcome"], record["stop_reason"]), ("stopped", "abandoned"))
        self.assertEqual(coffee_state(again)[0], "stopped")


class GuardTests(unittest.TestCase):
    def test_no_attribute_or_method_shadows_appdaemon(self):
        source = (Path(cm.__file__)).read_text()
        tree = ast.parse(source)
        assigned = {node.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                    and isinstance(node.value, ast.Name) and node.value.id == "self"}
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "CoffeeMachine")
        defined = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertEqual(sorted((assigned | defined) & APPDAEMON_NAMES), [])

    def test_log_lines_are_ascii(self):
        app = fake.make_app(recipes=({**fake.USUAL, "name": "Café — 4 Pour Recipe"},),
                            usual_name="Café — 4 Pour Recipe")
        brew(app, user=MIKKEL)
        fake.op(app, "beans_add", {"grams": 250, "new_name": "Käffe"})
        self.assertTrue(app.logs)
        for level, message in app.logs:
            self.assertTrue(message.isascii(), message)

    def test_the_yaml_has_no_log_key_and_flat_values(self):
        self.assertNotIn("log", fake.CONFIG)
        self.assertEqual(fake.CONFIG["dependencies"], "MobileNotifier")
        for key, value in fake.CONFIG.items():
            self.assertNotIsInstance(value, dict, key)


if __name__ == "__main__":
    unittest.main()
