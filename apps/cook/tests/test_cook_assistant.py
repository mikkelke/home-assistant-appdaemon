import asyncio
import ast
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass", "appdaemon.plugins.hass.hassapi"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["appdaemon.plugins.hass.hassapi"].Hass = object

import cook_assistant as ca
import cook_io as storage

NOW = 1800000000


def full(state, attributes=None, changed=None):
    return {"state": str(state), "attributes": attributes or {},
            "last_changed": datetime.fromtimestamp(changed or NOW - 3600, timezone.utc).isoformat()}


class Fake:
    def __init__(self, root, states=None, now=NOW):
        app = ca.CookAssistant.__new__(ca.CookAssistant)
        self.app = app
        self.now = now
        self.states = {"climate.anova_cooker": full("off", {"current_temperature": 20, "temperature": 58}),
                       "binary_sensor.anova_cooker_low_water": full("off"),
                       "binary_sensor.oppopad4pro_interactive": full("off")}
        self.states.update(states or {})
        self.published, self.history, self.tasks, self.calls = {}, [], [], []
        self.events, self.logs, self.listeners, self.timers, self.pushes = [], [], [], [], []
        self.responses = {}
        app.args = {"state_file": str(root / "cook_state.json"), "ledger_file": str(root / "cook_log.json"),
                    "media_dir": str(root / "media")}
        app.get_now_ts = lambda: self.now
        app.get_state = lambda entity, **kw: self.states.get(entity)
        app.set_state = self.set_state
        app.get_app = lambda name: self
        app.listen_state = lambda callback, entity, **kw: self.listeners.append((entity, callback))
        app.listen_event = lambda callback, event, **kw: self.events.append((event, callback))
        app.run_every = lambda *args, **kw: self.timers.append(args)
        app.create_task = self.tasks.append
        app.call_service = self.call_service
        app.fire_event = lambda *args, **kw: self.calls.append((args[0], kw))
        app.log = lambda *args, **kw: self.logs.append(args)
        app.initialize()
        self.drain()

    async def notify(self, **kwargs):
        self.pushes.append(kwargs)
        return 1

    async def call_service(self, service, **data):
        self.calls.append((service, data))
        result = self.responses.get(service, {"success": True})
        if isinstance(result, Exception):
            raise result
        return result

    def set_state(self, entity, **data):
        self.published[entity] = data
        self.history.append((self.now, entity, data))

    def drain(self):
        while self.tasks:
            asyncio.run(self.tasks.pop(0))

    def change(self, entity, state, attrs=None):
        old = self.states.get(entity)
        new = full(state, attrs, self.now)
        self.states[entity] = new
        self.app._on_value(entity, "all", old, new, {})
        self.drain()

    def command(self, op, data=None):
        self.app._on_command("cook_command", {"op": op, "data": json.dumps(data or {})}, {})
        self.drain()

    def tick(self, seconds):
        self.now += seconds
        self.app._on_tick({})
        self.drain()

    def bath(self, temp, state="heat", target=58):
        self.change("climate.anova_cooker", state, {"current_temperature": temp, "temperature": target})

    def probe(self, core, ambient, needles=None):
        prefix = "sensor.cookperfect_0ca6_"
        values = {"core_temperature": core, "ambient_temperature": ambient}
        values.update({f"needle_{index + 1}": value for index, value in enumerate(needles or [core] * 4)})
        for key, value in values.items():
            self.states[prefix + key] = full(value, changed=self.now)
            self.app._on_value(prefix + key, "all", None, self.states[prefix + key], {})
        self.drain()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = Fake(self.root)

    def test_registration_and_contract(self):
        config = yaml.safe_load((HERE / "cook_assistant.yaml").read_text())["CookAssistant"]
        self.assertEqual(config["dependencies"], "MobileNotifier")
        notifier = yaml.safe_load((HERE.parent / "notify" / "mobile_notifier.yaml").read_text())
        self.assertEqual(notifier["MobileNotifier"]["category_audience"]["cook"], ["mikkel"])
        self.assertEqual(set(self.fake.published), {"sensor.cook_state", "sensor.cook_live", "sensor.cook_log"})
        self.assertTrue(all(row["replace"] for row in self.fake.published.values()))
        self.assertEqual({event for event, _ in self.fake.events},
                         {"cook_command", "mobile_app_notification_action", "plugin_started"})
        self.assertTrue((self.root / "cook_state.json").exists())
        self.assertTrue((self.root / "cook_log.json").exists())

    def test_device_start_is_ordered_and_uses_no_anova_timer(self):
        self.fake.command("bath_start", {"target": 61})
        self.assertEqual(self.fake.calls, [
            ("climate/set_temperature", {"entity_id": "climate.anova_cooker", "temperature": 61}),
            ("climate/set_hvac_mode", {"entity_id": "climate.anova_cooker", "hvac_mode": "heat"})])
        self.fake.change("binary_sensor.anova_cooker_low_water", "on")
        self.fake.command("silence")
        self.assertEqual(self.fake.calls[-1], ("button/press", {"entity_id": "button.anova_cooker_silence_alarm"}))

    def test_refused_temperature_prevents_start_and_exceptions_are_logged(self):
        self.fake.responses["climate/set_temperature"] = {"success": False}
        self.fake.command("bath_start", {"target": 61})
        self.assertEqual([service for service, _ in self.fake.calls], ["climate/set_temperature"])
        self.fake.responses["climate/set_temperature"] = RuntimeError("offline")
        self.fake.command("bath_start", {"target": 62})
        self.assertTrue(any("failed" in str(row) for row in self.fake.logs))

    def test_failed_stop_waits_for_observed_device_state(self):
        self.fake.bath(58)
        self.fake.command("bag_in", {"minutes": 150})
        self.fake.responses["button/press"] = {"success": False}
        self.fake.command("bath_stop")
        self.assertEqual(self.fake.app._tracker.bath["phase"], "cooking")
        self.fake.bath(58, state="off")
        self.assertEqual(self.fake.app._tracker.bath["phase"], "interrupted")

    def test_device_commands_cannot_interleave_start_and_stop(self):
        self.fake.bath(20)
        self.fake.calls.clear()

        async def run():
            await asyncio.gather(
                self.fake.app._device_command({"op": "bath_start", "target": 60}),
                self.fake.app._device_command({"op": "bath_stop"}))

        asyncio.run(run())
        self.assertEqual([service for service, _ in self.fake.calls],
                         ["climate/set_temperature", "climate/set_hvac_mode", "button/press"])

    def test_callback_values_win_over_lagging_store(self):
        self.fake.app._on_value("climate.anova_cooker", "all", None,
                                full("heat", {"current_temperature": 58, "temperature": 58}), {})
        self.fake.drain()
        self.assertEqual(self.fake.app._tracker.bath["phase"], "ready")
        self.assertEqual(self.fake.states["climate.anova_cooker"]["state"], "off")

    def test_push_actions_restore_push_once_and_tablet_idle(self):
        self.fake.bath(58)
        self.assertEqual(len(self.fake.pushes), 1)
        push = self.fake.pushes[0]
        self.assertEqual((push["category"], push["target"], push["link"]),
                         ("cook", ["mikkel"], "/dashboard-rooftop/home#room=kitchen"))
        self.assertEqual(push["data"]["data"]["actions"][0]["action"], "COOK_BAG_IN")
        tablet = [data for service, data in self.fake.calls if service == "notify/mobile_app_oppopad4pro"]
        self.assertEqual(tablet[0]["message"], "command_screen_on")
        self.assertIn("#cook&takeover=", tablet[1]["data"]["command"])
        again = Fake(self.root, self.fake.states, self.fake.now)
        self.assertEqual(again.pushes, [])
        again.app._on_push_action("mobile_app_notification_action", {"action": "COOK_BAG_IN"}, {})
        again.drain()
        self.assertEqual(again.app._tracker.bath["phase"], "cooking")
        self.assertEqual(again.app._tracker.bath["minutes"], 150)

    def test_recently_active_tablet_is_not_woken(self):
        self.fake.change("binary_sensor.oppopad4pro_interactive", "on")
        self.fake.bath(58)
        self.assertFalse(any(service == "notify/mobile_app_oppopad4pro" for service, _ in self.fake.calls))

    def test_offline_restart_never_turns_interrupted_cook_into_done(self):
        self.fake.bath(58)
        self.fake.command("bag_in", {"minutes": 1})
        self.fake.tick(10)
        for state in ("off", "unavailable"):
            with self.subTest(state=state):
                states = dict(self.fake.states)
                states["climate.anova_cooker"] = full(state, changed=NOW + 20)
                self.fake.app.terminate()
                again = Fake(self.root, states, NOW + 1000)
                self.assertEqual(again.app._tracker.bath["phase"], "interrupted")
                self.assertFalse(any("Sous vide done" in push["title"] for push in again.pushes))

    def test_restart_with_heat_holding_completes_elapsed_clock(self):
        self.fake.bath(58)
        self.fake.command("bag_in", {"minutes": 1})
        self.fake.app.terminate()
        again = Fake(self.root, self.fake.states, NOW + 1000)
        self.assertEqual(again.app._tracker.bath["phase"], "done")
        self.assertTrue(any("Sous vide done" in push["title"] for push in again.pushes))

    def test_locked_needle_outage_survives_restart_even_when_min_sensor_is_numeric(self):
        self.fake.probe(51, 100, [54, 51, 55, 56])
        self.fake.tick(30)
        self.fake.probe(51, 100, [54, 51, 55, 56])
        self.fake.command("probe_pulled")
        self.assertEqual(self.fake.app._tracker.probe["core_index"], 1)
        self.fake.app.terminate()
        states = dict(self.fake.states)
        states["sensor.cookperfect_0ca6_needle_2"] = full("unavailable", changed=NOW + 40)
        again = Fake(self.root, states, NOW + 1000)
        self.assertEqual(again.app._tracker.probe["phase"], "off")
        self.assertEqual(again.app._records[-1]["kind"], "probe")
        self.assertEqual(again.pushes, [])

    def test_done_holds_heat_and_ledger_curve_survive_restart(self):
        self.fake.bath(58)
        self.fake.command("bag_in", {"minutes": 1})
        cook_id = self.fake.app._tracker.bath["id"]
        self.fake.tick(30)
        self.fake.tick(30)
        self.assertEqual(self.fake.app._tracker.bath["phase"], "done")
        self.assertFalse(any(service == "button/press" for service, _ in self.fake.calls))
        self.fake.command("chill")
        self.fake.tick(900)
        self.assertEqual(self.fake.app._tracker.bath["phase"], "chilled")
        self.fake.command("bath_clear")
        record = storage.read_json(self.root / "cook_log.json", {})["cooks"][-1]
        self.assertEqual((record["id"], record["kind"]), (cook_id, "bath"))
        self.assertIn("end", record)
        self.assertTrue(storage.read_json(self.root / "media" / f"{cook_id}.json", {})["samples"])
        again = Fake(self.root, self.fake.states, self.fake.now)
        self.assertEqual(again.app._tracker.bath["phase"], "off")
        self.assertEqual(len(again.app._records), 1)

    def test_probe_shadow_record_and_house_event(self):
        self.fake.probe(49, 100, [49, 51, 52, 53])
        self.fake.tick(30)
        self.fake.probe(50, 100, [50, 52, 53, 54])
        self.assertEqual(self.fake.app._tracker.probe["phase"], "pull")
        self.fake.command("probe_pulled")
        for _ in range(15):
            self.fake.tick(60)
            self.fake.probe(54, 100, [54, 40, 39, 38])
        self.assertEqual(self.fake.app._tracker.probe["phase"], "carve")
        self.assertTrue(any(service == "house_events_report" for service, _ in self.fake.calls))
        self.fake.command("probe_done")
        record = storage.read_json(self.root / "cook_log.json", {})["cooks"][-1]
        self.assertEqual(record["kind"], "probe")
        self.assertTrue(record["shadow"])

    def test_live_is_throttled_and_restart_gap_keeps_fixed_step(self):
        self.fake.bath(20)
        before = len([row for row in self.fake.history if row[1] == "sensor.cook_live"])
        for _ in range(10):
            self.fake.bath(21)
        self.assertEqual(len([row for row in self.fake.history if row[1] == "sensor.cook_live"]), before)
        self.fake.tick(30)
        self.fake.app.terminate()
        again = Fake(self.root, self.fake.states, self.fake.now + 86400)
        samples = again.app._samples
        self.assertLessEqual(len(samples), 480)
        self.assertTrue(all(b["at"] - a["at"] == 30 for a, b in zip(samples, samples[1:])))

    def test_bad_commands_are_ignored_and_ha_restart_republishes(self):
        for data in ([], "[]", "bad json", 3):
            self.fake.app._on_command("cook_command", {"op": "bag_in", "data": data}, {})
        self.fake.command("unknown")
        self.assertEqual(self.fake.app._tracker.bath["phase"], "off")
        before = len(self.fake.history)
        self.fake.app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(self.fake.history) - before, 3)

    def test_corrupt_runtime_file_is_preserved(self):
        (self.root / "cook_state.json").write_text("broken")
        Fake(self.root)
        self.assertEqual(len(list(self.root.glob("cook_state.json.corrupt-*"))), 1)

    def test_short_episode_still_has_curve_file(self):
        self.fake.bath(20)
        cook_id = self.fake.app._tracker.bath["id"]
        self.fake.command("bath_clear")
        self.assertTrue(storage.read_json(self.root / "media" / f"{cook_id}.json", {})["samples"])

    def test_atomic_write_failure_preserves_previous_file(self):
        path = self.root / "atomic.json"
        storage.write_json(path, {"old": 1})
        with patch.object(storage.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                storage.write_json(path, {"new": 2})
        self.assertEqual(storage.read_json(path, None), {"old": 1})
        self.assertFalse(path.with_name("atomic.json.tmp").exists())

    def test_no_appdaemon_api_shadowing(self):
        reserved = {"_log", "_namespace", "_plugin", "_config_model", "config", "name", "entities", "now"}
        tree = ast.parse((HERE / "cook_assistant.py").read_text())
        assigned = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Store)}
        self.assertFalse(assigned & reserved)


if __name__ == "__main__":
    unittest.main()
