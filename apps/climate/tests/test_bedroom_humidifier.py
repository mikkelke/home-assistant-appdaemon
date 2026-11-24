# tests/test_bedroom_humidifier.py - BedroomHumidifier: night control with hysteresis, window and
# unreadable inputs, the choice between the humidity sources by freshness, morning drying with its
# off after two hours, manual use left alone, one water push per empty spell, command retry and
# pause, restart survival, the run log and the published sensor. The world is a fake Home Assistant
# whose humidifier obeys service calls and, like the real one, reports state off with mode
# drying_filter after every turn_off; events caused by the app's own commands arrive after the
# callback that sent them has returned, as in AppDaemon.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

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

import bedroom_humidifier as bh  # noqa: E402

YAML_FILE = Path(__file__).resolve().parents[1] / "bedroom_humidifier.yaml"
YAML_ARGS = yaml.safe_load(YAML_FILE.read_text())["BedroomHumidifier"]

HUMIDIFIER = "humidifier.evaporative_humidifier_271a"
WATER = "sensor.evaporative_humidifier_271a_water_level"
PRIMARY = "sensor.bedroom_seated_thermometer_humidity"
FALLBACK = "sensor.airdot_b49928_humidity"
SOURCES = (PRIMARY, FALLBACK)
SLEEP = "input_boolean.mikkel_sleep_mode"
WINDOW = "binary_sensor.bedroom_window_contact"
PERSON = "person.mikkel"
HEALTH = "sensor.bedroom_humidifier"

MIN = 60
H = 3600
T0 = datetime(2026, 11, 24, 22, 0, tzinfo=bh.HOUSE_TZ).timestamp()

TURN_ON = ("humidifier/turn_on", {})
TURN_OFF = ("humidifier/turn_off", {})
SLEEP_MODE = ("humidifier/set_mode", {"mode": "sleep"})
DRYING_MODE = ("humidifier/set_mode", {"mode": "drying_filter"})


NO_REPLY = object()
DERIVED = object()


class World:
    """Home Assistant as the app sees it, plus the stubs of AppDaemon's primitives."""

    def __init__(self, tmp, now=T0, args=None, **states):
        self.clock = now
        self.states = {SLEEP: "off", WINDOW: "off", PRIMARY: "35.0", FALLBACK: "unavailable", PERSON: "home"}
        self.states.update(states)
        self.stamps = {}
        self.device = {"state": "off", "mode": "quiet"}
        self.level = "high"
        self.level_changed = now - 3600
        self.calls = []
        self.timeouts = []
        self.timers = []
        self.logs = []
        self.fail = 0
        self.raise_next = None
        self.reply = NO_REPLY
        self.silent = False
        self.dry_needs_on = False
        self.auto_dry = True
        self.action = DERIVED
        self.device_event = False
        self.notifier = MagicMock()
        self.notifier.notify = AsyncMock(return_value=1)
        self.args = dict(YAML_ARGS)
        self.args["state_file"] = str(Path(tmp) / "state.json")
        self.args["log_file"] = str(Path(tmp) / "run_log.jsonl")
        self.args["humidity_max_age_min"] = 30
        self.args.update(args or {})
        self.state_file = Path(self.args["state_file"])
        self.log_file = Path(self.args["log_file"])
        self.app = None
        self.start()

    def start(self):
        """A fresh app instance on the same world and the same files, as after a restart."""
        app = bh.BedroomHumidifier.__new__(bh.BedroomHumidifier)
        app.args = dict(self.args)
        app._now = lambda: self.clock
        app.get_state = self._get_state
        app.call_service = self._call_service
        app.set_state = MagicMock()
        app.listen_state = MagicMock()
        app.run_every = MagicMock()
        app.run_in = lambda callback, delay, **kw: self.timers.append((callback.__name__, delay))
        app.log = lambda msg, level="INFO", **kw: self.logs.append((level, msg))
        app.get_app = MagicMock(return_value=self.notifier)
        app.create_task = lambda coro: asyncio.run(coro)
        self.app = app
        app.initialize()
        self._drain()

    # ---- the stubs ----

    def _get_state(self, entity, attribute=None, **kw):
        if entity == HUMIDIFIER and attribute == "all":
            attrs = {"mode": self.device["mode"]}
            action = self._action()
            if action is not None:
                attrs["action"] = action
            return {"state": self.device["state"], "attributes": attrs}
        if entity == WATER and attribute == "all":
            changed = datetime.fromtimestamp(self.level_changed, timezone.utc).isoformat()
            return {"state": self.level, "last_changed": changed}
        if entity in SOURCES and attribute == "all":
            return self._source(entity)
        if attribute is not None:
            raise AssertionError(f"unexpected get_state({entity}, attribute={attribute})")
        return self.states.get(entity)

    def _source(self, entity):
        """A humidity source as AppDaemon caches it. Unless the test made it quiet, it reports right now."""
        if entity not in self.states:
            return None
        reported, updated = self.stamps.get(entity, (self.clock, self.clock))
        full = {"state": self.states[entity]}
        for key, stamp in (("last_reported", reported), ("last_updated", updated)):
            if stamp is not None:
                full[key] = stamp if isinstance(stamp, str) else datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        return full

    def _action(self):
        """The action attribute as Home Assistant derives it from the power and the mode."""
        if self.action is not DERIVED:
            return self.action
        if self.device["state"] != "on":
            return "off"
        return "drying" if self.device["mode"] == "drying_filter" else "humidifying"

    def _call_service(self, service, entity_id=None, hass_timeout=None, **data):
        assert entity_id == HUMIDIFIER
        self.calls.append((service, data))
        self.timeouts.append(hass_timeout)
        if self.raise_next:
            error, self.raise_next = self.raise_next, None
            raise error
        if self.reply is not NO_REPLY:
            return self.reply
        if self.fail:
            self.fail -= 1
            return {"success": False, "error": {"code": "unknown_error", "message": "BLE failed"}, "ad_status": "OK"}
        if not self.silent:
            self._apply(service, data)
        return {"success": True, "ad_status": "OK"}

    def _apply(self, service, data):
        device = self.device
        before = dict(device)
        if service == "humidifier/turn_on":
            device["state"] = "on"
        elif service == "humidifier/turn_off":
            device["state"] = "off"
            if self.auto_dry:
                device["mode"] = "drying_filter"
        elif service == "humidifier/set_mode":
            device["mode"] = data["mode"]
            if data["mode"] == "drying_filter" and not (device["state"] == "off" and self.dry_needs_on):
                device["state"] = "on"
        if device != before:
            self.device_event = True

    def _drain(self):
        for _ in range(10):
            if not self.device_event:
                return
            self.device_event = False
            self.app._on_change(HUMIDIFIER, "all", None, None, {})
        raise AssertionError("device events never settle")

    # ---- what happens in the house ----

    def event(self, entity, value):
        self.states[entity] = value
        if entity in self.stamps:
            self.stamps[entity] = (self.clock, self.clock)
        self.app._on_change(entity, "state", None, value, {})
        self._drain()

    def quiet(self, source, seconds=0):
        """The source last reported `seconds` ago and says nothing more until its next event."""
        self.stamps[source] = (self.clock - seconds, self.clock - seconds)

    def sleep(self, on):
        self.event(SLEEP, "on" if on else "off")

    def humidity(self, value):
        self.event(PRIMARY, str(value))

    def fallback(self, value):
        self.event(FALLBACK, str(value))

    def window(self, open_):
        self.event(WINDOW, "on" if open_ else "off")

    def person(self, state):
        self.event(PERSON, state)

    def tank(self, level):
        self.level, self.level_changed = level, self.clock
        self.app._on_change(WATER, "state", None, level, {})
        self._drain()

    def by_hand(self, state, mode):
        self.device.update(state=state, mode=mode)
        self.app._on_change(HUMIDIFIER, "all", None, None, {})
        self._drain()

    def tick(self, seconds=0):
        self.clock += seconds
        self.app._tick({})
        self._drain()

    def tick_for(self, seconds, step=5 * MIN):
        """Five-minute checks until `seconds` have passed."""
        end = self.clock + seconds
        while self.clock < end:
            self.tick(min(step, end - self.clock))

    # ---- what the tests look at ----

    def services(self):
        return [service for service, _ in self.calls]

    def sensor(self):
        call = self.app.set_state.call_args_list[-1]
        return call.kwargs["state"], call.kwargs["attributes"]

    def lines(self, level=None, containing=""):
        return [msg for lvl, msg in self.logs if (level is None or lvl == level) and containing in msg]

    def run_log(self):
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]

    def saved(self):
        return json.loads(self.state_file.read_text())


class Base(unittest.TestCase):
    def world(self, **kw):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return World(tmp, **kw)

    def night(self, humidity=35.0, **kw):
        """Sleep mode on, window closed, a dry room: the humidifier is running in sleep mode."""
        w = self.world(**kw)
        w.humidity(humidity)
        w.sleep(True)
        return w


class NightControl(Base):
    def test_nothing_happens_while_sleep_mode_is_off(self):
        w = self.world(**{PRIMARY: "30.0"})
        w.tick_for(3 * H)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.sensor()[0], "off")

    def test_starts_below_forty_in_sleep_mode(self):
        w = self.world()
        w.humidity(39.9)
        w.sleep(True)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])
        self.assertEqual(w.device, {"state": "on", "mode": "sleep"})
        self.assertEqual(w.sensor()[0], "humidifying")

    def test_no_start_at_exactly_forty(self):
        w = self.world()
        w.humidity(40.0)
        w.sleep(True)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.sensor()[0], "off")

    def test_stops_at_forty_two_and_not_before(self):
        w = self.night(38)
        w.humidity(41.9)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])
        w.humidity(42.0)
        self.assertEqual(w.calls[2:], [TURN_OFF])
        self.assertEqual(w.device["state"], "off")

    def test_hysteresis_between_forty_and_forty_two(self):
        w = self.night(39)
        w.humidity(41)
        w.humidity(40)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE], "keeps running in the band")
        w.humidity(42)
        self.assertEqual(w.calls[2:], [TURN_OFF])
        w.humidity(41)
        w.humidity(40)
        self.assertEqual(w.calls[3:], [], "stays off in the band")
        w.humidity(39.9)
        self.assertEqual(w.calls[3:], [TURN_ON, SLEEP_MODE])

    def test_sleep_mode_turning_on_inside_the_band_starts_nothing(self):
        w = self.world()
        w.humidity(41)
        w.sleep(True)
        self.assertEqual(w.calls, [])

    def test_five_minute_check_notices_humidity_without_an_event(self):
        w = self.world()
        w.sleep(True)
        w.states[PRIMARY] = "36.0"
        w.tick(5 * MIN)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])

    def test_commands_wait_for_the_answer_long_enough_for_a_slow_ble_link(self):
        w = self.night(38)
        self.assertEqual(set(w.timeouts), {bh.CALL_TIMEOUT_S})
        self.assertGreaterEqual(bh.CALL_TIMEOUT_S, 30)

    def test_another_mode_while_sleep_mode_is_on_is_set_back_without_a_turn_on(self):
        w = self.world()
        w.by_hand("on", "high")
        w.humidity(38)
        w.sleep(True)
        self.assertEqual(w.calls, [SLEEP_MODE])
        self.assertEqual(w.device["mode"], "sleep")

    def test_a_manual_run_in_the_band_is_quieted_not_stopped(self):
        w = self.world()
        w.by_hand("on", "high")
        w.humidity(41)
        w.sleep(True)
        self.assertEqual(w.calls, [SLEEP_MODE])

    def test_a_manual_run_is_stopped_at_forty_two_when_sleep_mode_takes_over(self):
        w = self.world()
        w.by_hand("on", "high")
        w.humidity(45)
        w.sleep(True)
        self.assertEqual(w.calls, [TURN_OFF])

    def test_unreadable_humidity_leaves_a_running_humidifier_alone_and_logs_once(self):
        for bad in ("unavailable", "unknown", "", "abc", "nan"):
            with self.subTest(bad=bad):
                w = self.night(38)
                n = len(w.calls)
                w.event(PRIMARY, bad)
                w.tick_for(30 * MIN)
                self.assertEqual(len(w.calls), n)
                self.assertEqual(len(w.lines("WARNING", "bedroom humidity unreadable")), 1)
                w.humidity(42)
                self.assertEqual(w.calls[n:], [TURN_OFF])
                self.assertTrue(w.lines("INFO", "readable again"))

    def test_unreadable_humidity_does_not_start_a_stopped_one(self):
        w = self.world()
        w.event(PRIMARY, "unavailable")
        w.sleep(True)
        w.tick_for(H)
        self.assertEqual(w.calls, [])
        self.assertIn("unreadable", w.sensor()[1]["reason"])

    def test_sleep_mode_going_unavailable_keeps_the_last_reading(self):
        w = self.world()
        w.sleep(True)
        w.event(SLEEP, "unavailable")
        w.humidity(38)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])

    def test_unavailable_humidifier_gets_no_commands_and_is_unknown(self):
        w = self.night(38)
        n = len(w.calls)
        w.device["state"] = "unavailable"
        w.tick(5 * MIN)
        w.humidity(30)
        self.assertEqual(len(w.calls), n)
        self.assertEqual(w.sensor()[0], "unknown")


class Window(Base):
    def test_window_opening_stops_the_humidifier(self):
        w = self.night(38)
        w.window(True)
        self.assertEqual(w.calls[2:], [TURN_OFF])
        self.assertEqual(w.sensor()[0], "off")
        self.assertEqual(w.sensor()[1]["reason"], "Window open")

    def test_open_window_at_the_start_of_the_night_blocks_the_start(self):
        w = self.world()
        w.humidity(30)
        w.window(True)
        w.sleep(True)
        w.tick_for(H)
        self.assertEqual(w.calls, [])

    def test_closing_the_window_starts_again_when_the_room_is_dry(self):
        w = self.night(38)
        w.window(True)
        w.window(False)
        self.assertEqual(w.calls[3:], [TURN_ON, SLEEP_MODE])

    def test_closing_the_window_inside_the_band_stays_off(self):
        w = self.night(38)
        w.window(True)
        w.humidity(41)
        w.window(False)
        self.assertEqual(w.calls[3:], [])

    def test_unreadable_window_holds_the_humidifier_as_it_is(self):
        for kept in ("running", "stopped"):
            with self.subTest(kept=kept):
                w = self.night(38) if kept == "running" else self.world()
                w.sleep(True)
                n = len(w.calls)
                w.event(WINDOW, "unavailable")
                w.humidity(30 if kept == "stopped" else 50)
                w.tick_for(30 * MIN)
                self.assertEqual(len(w.calls), n)
                self.assertTrue(w.lines("WARNING", "window unreadable"))

    def test_window_open_by_day_changes_nothing(self):
        w = self.world()
        w.by_hand("on", "high")
        w.window(True)
        self.assertEqual(w.calls, [])


class MorningDrying(Base):
    def morning(self, w):
        """The night's run ends with the humidifier still on, then sleep mode turns off."""
        w.tick(5 * H)
        w.sleep(False)

    def test_a_night_with_a_run_dries_the_wick_when_sleep_mode_turns_off(self):
        w = self.night(38)
        self.morning(w)
        self.assertEqual(w.calls[2:], [DRYING_MODE])
        self.assertNotIn(TURN_OFF, w.calls)
        self.assertEqual(w.device, {"state": "on", "mode": "drying_filter"})
        state, attrs = w.sensor()
        self.assertEqual(state, "drying")
        self.assertIn("Drying", attrs["reason"])

    def test_drying_is_turned_off_after_two_hours_and_not_before(self):
        w = self.night(38)
        self.morning(w)
        w.tick_for(2 * H - 10 * MIN)
        self.assertNotIn(TURN_OFF, w.calls)
        w.tick(10 * MIN)
        self.assertEqual(w.calls[3:], [TURN_OFF])
        self.assertEqual(w.device["state"], "off")
        w.tick_for(3 * H)
        self.assertEqual(w.calls[3:], [TURN_OFF], "dried once, not again")

    def test_the_two_hour_timer_is_scheduled(self):
        w = self.night(38)
        self.morning(w)
        self.assertIn(("_tick", 2 * H + 5), w.timers)

    def test_a_run_that_ended_before_morning_still_dries_the_wick(self):
        w = self.night(38)
        w.humidity(43)
        w.tick(3 * H)
        w.sleep(False)
        self.assertEqual(w.calls[3:], [DRYING_MODE])
        self.assertEqual(w.device, {"state": "on", "mode": "drying_filter"})

    def test_a_run_the_app_did_not_start_counts_when_it_runs_in_sleep_mode(self):
        w = self.world()
        w.by_hand("on", "sleep")
        w.humidity(41)
        w.sleep(True)
        self.assertEqual(w.calls, [], "in the band and already quiet: nothing to send")
        w.tick(5 * H)
        w.sleep(False)
        self.assertEqual(w.calls, [DRYING_MODE])

    def test_sleep_mode_passing_through_unavailable_still_gives_the_morning_edge(self):
        w = self.night(38)
        w.tick(5 * H)
        w.event(SLEEP, "unavailable")
        self.assertEqual(w.calls[2:], [])
        w.sleep(False)
        self.assertEqual(w.calls[2:], [DRYING_MODE])

    def test_a_humidifier_that_drops_out_mid_run_does_not_end_the_run(self):
        w = self.night(38)
        w.tick(H)
        w.device["state"] = "unavailable"
        w.tick(5 * MIN)
        w.device["state"] = "on"
        w.tick(55 * MIN)
        self.assertEqual(w.run_log(), [])
        w.humidity(43)
        (record,) = w.run_log()
        self.assertEqual(record["minutes"], 120.0)

    def test_a_night_without_a_run_starts_nothing(self):
        w = self.world()
        w.humidity(45)
        w.sleep(True)
        w.tick(8 * H)
        w.sleep(False)
        w.tick_for(3 * H)
        self.assertEqual(w.calls, [])

    def test_a_manual_daytime_run_is_not_a_night_run(self):
        w = self.world()
        w.by_hand("on", "high")
        w.tick(H)
        w.by_hand("off", "high")
        w.humidity(45)
        w.sleep(True)
        w.tick(8 * H)
        w.sleep(False)
        self.assertEqual(w.calls, [])

    def test_drying_never_runs_while_sleep_mode_is_on(self):
        w = self.night(38)
        self.morning(w)
        w.humidity(45)
        w.sleep(True)
        self.assertEqual(w.calls[3:], [TURN_OFF])
        self.assertEqual(w.device["state"], "off")
        w.tick_for(3 * H)
        self.assertEqual(w.calls[3:], [TURN_OFF])

    def test_sleep_mode_back_on_in_a_dry_room_swaps_drying_for_the_night_mode(self):
        w = self.night(38)
        self.morning(w)
        w.sleep(True)
        self.assertEqual(w.calls[3:], [SLEEP_MODE])
        self.assertEqual(w.device, {"state": "on", "mode": "sleep"})

    def test_cancelled_drying_leaves_the_wick_for_the_next_morning(self):
        w = self.night(38)
        self.morning(w)
        w.humidity(45)
        w.sleep(True)
        w.tick(8 * H)
        w.sleep(False)
        self.assertEqual(w.calls[4:], [DRYING_MODE])

    def test_drying_that_finished_by_itself_is_not_turned_off_again(self):
        w = self.night(38)
        self.morning(w)
        w.by_hand("off", "drying_filter")
        w.tick_for(3 * H)
        self.assertNotIn(TURN_OFF, w.calls)

    def test_drying_changed_by_hand_is_not_turned_off(self):
        w = self.night(38)
        self.morning(w)
        w.by_hand("on", "high")
        w.tick_for(3 * H)
        self.assertNotIn(TURN_OFF, w.calls)
        self.assertEqual(w.sensor()[0], "manual")

    def test_a_drying_command_that_did_not_start_the_humidifier_is_sent_again_with_a_turn_on(self):
        w = self.night(38)
        w.humidity(43)
        w.tick(3 * H)
        w.dry_needs_on = True
        w.sleep(False)
        self.assertEqual(w.calls[3:], [DRYING_MODE])
        self.assertEqual(w.device["state"], "off")
        w.tick(bh.RETRY_S)
        self.assertEqual(w.calls[4:], [TURN_ON, DRYING_MODE])
        self.assertEqual(w.device, {"state": "on", "mode": "drying_filter"})
        w.tick_for(H)
        self.assertEqual(len(w.calls), 6)

    def test_a_drying_that_never_starts_is_given_up_until_the_next_morning(self):
        w = self.night(38)
        w.humidity(43)
        w.tick(3 * H)
        w.dry_needs_on = True
        w.silent = True
        w.sleep(False)
        w.tick(bh.RETRY_S)
        w.tick(5 * MIN)
        self.assertTrue(w.lines("ERROR", "dry did not take"))
        n = len(w.calls)
        w.tick_for(10 * H)
        self.assertEqual(len(w.calls), n)

    def test_an_empty_tank_skips_the_drying_and_keeps_the_wick_marked(self):
        w = self.night(38)
        w.tank("empty")
        w.tick(5 * H)
        w.sleep(False)
        self.assertNotIn(DRYING_MODE, w.calls)
        self.assertTrue(w.lines("WARNING", "drying skipped"))
        w.tank("high")
        w.tick_for(3 * H)
        self.assertNotIn(DRYING_MODE, w.calls, "not a drying in the afternoon after a refill")
        w.humidity(45)
        w.sleep(True)
        w.sleep(False)
        self.assertEqual(w.calls[-1], DRYING_MODE, "the next morning dries it")

    def test_a_short_sleep_mode_flap_in_the_night_dries_and_then_resumes_the_night(self):
        w = self.night(38)
        w.tick(3 * H)
        w.sleep(False)
        self.assertEqual(w.calls[2:], [DRYING_MODE])
        w.tick(MIN)
        w.sleep(True)
        self.assertEqual(w.calls[3:], [SLEEP_MODE])
        w.tick(5 * H)
        w.sleep(False)
        self.assertEqual(w.calls[4:], [DRYING_MODE], "the wick is still marked wet")


class ManualUse(Base):
    def test_a_humidifier_switched_on_by_hand_is_left_alone(self):
        w = self.world()
        w.by_hand("on", "high")
        w.tick_for(6 * H)
        w.humidity(30)
        w.humidity(60)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.sensor()[0], "manual")
        self.assertIn("high", w.sensor()[1]["reason"])

    def test_manual_use_is_logged_once(self):
        w = self.world()
        w.by_hand("on", "high")
        w.tick_for(3 * H)
        manual = w.lines("INFO", "-> manual")
        self.assertEqual(len(manual), 1)
        self.assertIn("by hand", manual[0])

    def test_start_up_with_the_humidifier_already_on_in_sleep_mode_does_nothing(self):
        w = self.world(**{PRIMARY: "30.0"})
        w.device.update(state="on", mode="sleep")
        w.start()
        w.tick_for(2 * H)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.sensor()[0], "manual")
        self.assertTrue(w.lines("INFO", "manual"))

    def test_a_drying_filter_started_by_hand_is_left_alone(self):
        w = self.world()
        w.by_hand("on", "drying_filter")
        w.tick_for(4 * H)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.sensor()[0], "drying")

    def test_manual_runs_are_not_in_the_run_log(self):
        w = self.world()
        w.by_hand("on", "high")
        w.tick(2 * H)
        w.by_hand("off", "high")
        self.assertEqual(w.run_log(), [])


class AutoDry(Base):
    """The device dries its wick by itself after a turn_off and then reports state off, mode drying_filter."""

    def test_a_stop_is_neither_repeated_nor_a_run_when_the_device_then_reports_drying_filter(self):
        w = self.night(38)
        w.humidity(43)
        self.assertEqual(w.device, {"state": "off", "mode": "drying_filter"})
        w.tick_for(3 * H)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE, TURN_OFF])
        state, attrs = w.sensor()
        self.assertEqual(state, "off")
        self.assertNotIn("mode", attrs)
        self.assertEqual([r["kind"] for r in w.run_log()], ["humidify"])
        self.assertIsNone(w.app._run)

    def test_the_mode_flipping_to_drying_filter_minutes_after_the_off_changes_nothing(self):
        w = self.world()
        w.auto_dry = False
        w.humidity(38)
        w.sleep(True)
        w.humidity(43)
        self.assertEqual(w.device, {"state": "off", "mode": "sleep"})
        calls, before = list(w.calls), w.sensor()
        w.tick(11 * MIN)
        w.by_hand("off", "drying_filter")
        w.tick_for(2 * H)
        self.assertEqual(w.calls, calls)
        self.assertEqual(w.sensor(), before)
        self.assertEqual([r["kind"] for r in w.run_log()], ["humidify"])

    def test_off_with_drying_filter_in_sleep_mode_is_not_a_night_run(self):
        w = self.world()
        w.humidity(45)
        w.by_hand("off", "drying_filter")
        w.sleep(True)
        w.tick_for(8 * H)
        w.sleep(False)
        w.tick_for(3 * H)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.run_log(), [])
        self.assertEqual(w.saved()["wick"], "dry")
        self.assertIsNone(w.saved()["run"])

    def test_off_with_drying_filter_by_day_is_not_manual_use(self):
        w = self.world()
        w.by_hand("off", "drying_filter")
        w.tick_for(3 * H)
        self.assertEqual(w.calls, [])
        state, attrs = w.sensor()
        self.assertEqual((state, attrs["reason"]), ("off", "Sleep mode is off"))
        self.assertEqual([line for line in w.lines() if "manual" in line or "by hand" in line], [])
        self.assertEqual(w.run_log(), [])

    def test_off_with_drying_filter_at_start_up_gets_no_command_in_any_state_of_the_room(self):
        for humidity in (41, 45):
            for asleep in (False, True):
                with self.subTest(humidity=humidity, asleep=asleep):
                    w = self.world(**{PRIMARY: str(humidity), SLEEP: "on" if asleep else "off"})
                    w.device.update(state="off", mode="drying_filter")
                    w.start()
                    w.tick_for(H)
                    self.assertEqual(w.calls, [])
                    self.assertEqual(w.sensor()[0], "off")

    def test_off_with_drying_filter_in_the_band_or_above_is_not_stopped_or_set_to_sleep(self):
        for humidity in (41, 45):
            with self.subTest(humidity=humidity):
                w = self.world()
                w.by_hand("off", "drying_filter")
                w.humidity(humidity)
                w.sleep(True)
                w.tick_for(H)
                self.assertEqual(w.calls, [])

    def test_off_with_drying_filter_in_a_dry_room_is_started_like_any_other_stopped_humidifier(self):
        w = self.world()
        w.by_hand("off", "drying_filter")
        w.humidity(35)
        w.sleep(True)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])
        self.assertEqual(w.device, {"state": "on", "mode": "sleep"})
        self.assertEqual(w.sensor()[0], "humidifying")

    def test_a_whole_night_and_morning_with_the_device_drying_itself_after_each_off(self):
        w = self.night(38)
        w.humidity(43)
        w.tick(2 * H)
        w.humidity(38)
        w.tick(3 * H)
        w.humidity(43)
        w.tick(2 * H)
        night = [TURN_ON, SLEEP_MODE, TURN_OFF, TURN_ON, SLEEP_MODE, TURN_OFF]
        self.assertEqual(w.calls, night)
        w.sleep(False)
        self.assertEqual(w.calls, night + [DRYING_MODE], "dried although the device already reads drying_filter")
        self.assertEqual(w.device, {"state": "on", "mode": "drying_filter"})
        self.assertEqual(w.sensor()[0], "drying")
        w.tick_for(2 * H + 10 * MIN)
        self.assertEqual(w.calls, night + [DRYING_MODE, TURN_OFF])
        w.tick_for(6 * H)
        self.assertEqual(len(w.calls), len(night) + 2)
        self.assertEqual(w.sensor()[0], "off")
        self.assertEqual(w.saved()["wick"], "dry")
        self.assertEqual([r["kind"] for r in w.run_log()], ["humidify", "humidify", "dry"])

    def test_off_with_drying_filter_after_the_drying_command_is_not_a_drying(self):
        w = self.night(38)
        w.humidity(43)
        w.tick(3 * H)
        w.dry_needs_on = True
        w.sleep(False)
        self.assertEqual(w.device, {"state": "off", "mode": "drying_filter"})
        self.assertEqual(w.sensor()[0], "off")
        self.assertIsNone(w.app._run)
        w.tick(bh.RETRY_S)
        self.assertEqual(w.calls[4:], [TURN_ON, DRYING_MODE])
        self.assertEqual(w.sensor()[0], "drying")

    def test_the_state_and_the_action_decide_and_the_mode_only_when_the_action_is_missing(self):
        cases = [
            ("off", "drying_filter", "off", "off"),
            ("off", "sleep", "off", "off"),
            ("off", "drying_filter", "drying", "off"),
            ("off", "sleep", "humidifying", "off"),
            ("off", "drying_filter", None, "off"),
            ("on", "drying_filter", "drying", "dry"),
            ("on", "sleep", "humidifying", "humidify"),
            ("on", "drying_filter", "humidifying", "humidify"),
            ("on", "sleep", "drying", "dry"),
            ("on", "drying_filter", None, "dry"),
            ("on", "sleep", None, "humidify"),
            ("unavailable", "sleep", "off", None),
            ("unknown", "drying_filter", None, None),
        ]
        for state, mode, action, kind in cases:
            with self.subTest(state=state, mode=mode, action=action):
                w = self.world()
                w.device.update(state=state, mode=mode)
                w.action = action
                self.assertEqual(w.app._read_device(), (kind, mode))

    def test_a_night_run_is_a_humidifier_that_is_on_and_humidifying_whatever_its_mode_reads(self):
        w = self.world()
        w.action = "humidifying"
        w.by_hand("on", "drying_filter")
        w.humidity(41)
        w.sleep(True)
        self.assertEqual(w.saved()["wick"], "wet")
        w.tick(5 * H)
        w.sleep(False)
        self.assertEqual(w.calls[-1], DRYING_MODE)


class Water(Base):
    def test_an_empty_tank_pushes_mikkel_once(self):
        w = self.world()
        w.tank("empty")
        w.notifier.notify.assert_awaited_once_with(
            title="Bedroom humidifier is out of water",
            message="The tank has read empty since 22:00.",
            target="mikkel",
            data={"tag": "bedroom_humidifier_water"},
        )
        w.tick_for(6 * H)
        self.assertEqual(w.notifier.notify.await_count, 1)
        self.assertEqual(w.sensor()[0], "no_water")
        self.assertIn("empty", w.sensor()[1]["reason"])

    def test_the_push_states_a_fact_and_gives_no_instruction(self):
        w = self.world()
        w.tank("empty")
        kwargs = w.notifier.notify.await_args.kwargs
        text = f"{kwargs['title']} {kwargs['message']}".lower()
        for word in ("please", "refill", "fill", "should", "need to"):
            self.assertNotIn(word, text)

    def test_low_is_not_empty(self):
        w = self.world()
        w.tank("low")
        w.tick(H)
        w.notifier.notify.assert_not_awaited()

    def test_rearms_only_when_the_level_is_medium_or_high(self):
        w = self.world()
        w.tank("empty")
        w.tank("low")
        w.tank("empty")
        self.assertEqual(w.notifier.notify.await_count, 1, "a level flickering between empty and low")
        w.tank("medium")
        w.tank("empty")
        self.assertEqual(w.notifier.notify.await_count, 2)
        w.tank("high")
        w.tank("empty")
        self.assertEqual(w.notifier.notify.await_count, 3)

    def test_when_mikkel_is_away_the_push_waits_for_his_arrival(self):
        w = self.world()
        w.person("not_home")
        w.tank("empty")
        w.tick_for(4 * H)
        w.notifier.notify.assert_not_awaited()
        self.assertEqual(len(w.lines("INFO", "on arrival")), 1)
        w.person("home")
        w.notifier.notify.assert_awaited_once()
        w.tick_for(2 * H)
        self.assertEqual(w.notifier.notify.await_count, 1)

    def test_a_tank_refilled_while_he_was_away_never_pushes(self):
        w = self.world()
        w.person("not_home")
        w.tank("empty")
        w.tank("high")
        w.person("home")
        w.notifier.notify.assert_not_awaited()

    def test_another_zone_is_not_home(self):
        w = self.world()
        w.person("office")
        w.tank("empty")
        w.notifier.notify.assert_not_awaited()

    def test_a_push_that_reached_nobody_is_retried_after_ten_minutes_not_on_every_check(self):
        w = self.world()
        w.notifier.notify.return_value = 0
        w.tank("empty")
        self.assertEqual(w.notifier.notify.await_count, 1)
        w.tick(5 * MIN)
        self.assertEqual(w.notifier.notify.await_count, 1)
        w.notifier.notify.return_value = 1
        w.tick(5 * MIN)
        self.assertEqual(w.notifier.notify.await_count, 2)
        w.tick_for(2 * H)
        self.assertEqual(w.notifier.notify.await_count, 2)

    def test_a_notify_error_is_retried_like_a_push_that_reached_nobody(self):
        w = self.world()
        w.notifier.notify.side_effect = RuntimeError("boom")
        w.tank("empty")
        self.assertTrue(w.lines("WARNING", "notify failed"))
        w.notifier.notify.side_effect = None
        w.tick(10 * MIN)
        self.assertEqual(w.notifier.notify.await_count, 2)

    def test_a_restart_in_an_empty_spell_does_not_push_again(self):
        w = self.world()
        w.tank("empty")
        w.start()
        w.tick_for(H)
        self.assertEqual(w.notifier.notify.await_count, 1)

    def test_an_empty_tank_holds_back_the_commands_that_need_water(self):
        w = self.world()
        w.tank("empty")
        w.humidity(30)
        w.sleep(True)
        w.tick_for(2 * H)
        self.assertEqual(w.calls, [])
        self.assertEqual(w.sensor()[0], "no_water")

    def test_an_empty_tank_still_allows_stopping(self):
        w = self.night(38)
        w.tank("empty")
        w.humidity(43)
        self.assertEqual(w.calls[2:], [TURN_OFF])

    def test_an_empty_tank_does_not_fill_the_log_with_failed_commands(self):
        w = self.world()
        w.tank("empty")
        w.humidity(30)
        w.sleep(True)
        w.tick_for(3 * H)
        self.assertEqual(w.lines("WARNING", "failed"), [])
        self.assertEqual(w.lines("ERROR"), [])

    def test_the_unreadable_level_is_not_empty(self):
        w = self.world()
        for bad in ("unavailable", "unknown"):
            w.tank(bad)
        w.notifier.notify.assert_not_awaited()


class CommandFailures(Base):
    def test_a_failed_start_is_retried_once_after_thirty_seconds(self):
        w = self.world()
        w.humidity(38)
        w.fail = 1
        w.sleep(True)
        self.assertEqual(w.calls, [TURN_ON])
        self.assertIn(("_tick", bh.RETRY_S), w.timers)
        self.assertTrue(w.lines("WARNING", "humidifier/turn_on failed: BLE failed"))
        w.tick(bh.RETRY_S)
        self.assertEqual(w.calls, [TURN_ON, TURN_ON, SLEEP_MODE])
        self.assertEqual(w.device, {"state": "on", "mode": "sleep"})
        w.tick_for(H)
        self.assertEqual(len(w.calls), 3)

    def test_events_between_the_tries_do_not_send_early(self):
        w = self.world()
        w.fail = 1
        w.humidity(38)
        w.sleep(True)
        w.tick(5)
        w.humidity(37)
        w.humidity(36)
        self.assertEqual(w.calls, [TURN_ON])
        w.tick(bh.RETRY_S)
        self.assertEqual(len(w.calls), 3)

    def test_two_failed_tries_pause_the_commands_for_fifteen_minutes(self):
        w = self.world()
        w.humidity(38)
        w.fail = 100
        w.sleep(True)
        w.tick(bh.RETRY_S)
        self.assertEqual(len(w.calls), 2)
        w.tick(5 * MIN)
        self.assertEqual(len(w.calls), 2)
        self.assertEqual(len(w.lines("ERROR", "start did not take")), 1)
        w.tick_for(bh.COOLDOWN_S - 1)
        self.assertEqual(len(w.calls), 2, "quiet during the pause")
        w.tick(1)
        self.assertEqual(len(w.calls), 3, "tries again when the pause is over")
        w.tick(bh.RETRY_S)
        self.assertEqual(len(w.calls), 4)

    def test_an_outage_all_night_stays_a_few_lines_an_hour(self):
        w = self.world()
        w.humidity(38)
        w.fail = 10_000
        w.sleep(True)
        w.tick_for(8 * H)
        self.assertLessEqual(len(w.calls), 8 * 4 * 2)
        self.assertLessEqual(len(w.lines("WARNING")) + len(w.lines("ERROR")), 8 * 4 * 3)

    def test_an_exception_from_the_service_call_counts_as_a_failure(self):
        w = self.world()
        w.humidity(38)
        w.raise_next = RuntimeError("socket closed")
        w.sleep(True)
        self.assertTrue(w.lines("WARNING", "RuntimeError: socket closed"))
        w.tick(bh.RETRY_S)
        self.assertEqual(w.device, {"state": "on", "mode": "sleep"})

    def test_timeouts_and_missing_answers_count_as_failures(self):
        for reply, text in (
            ({"success": False, "ad_status": "TIMEOUT"}, "TIMEOUT"),
            (None, "no answer"),
            ({"id": 3}, "unsuccessful"),
        ):
            with self.subTest(reply=reply):
                w = self.world()
                w.humidity(38)
                w.reply = reply
                w.sleep(True)
                self.assertTrue(w.lines("WARNING", text), w.logs)

    def test_a_command_reported_done_that_did_not_take_is_sent_again_once(self):
        w = self.world()
        w.humidity(38)
        w.silent = True
        w.sleep(True)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])
        w.tick(bh.RETRY_S)
        self.assertEqual(len(w.calls), 4)
        w.tick(5 * MIN)
        self.assertEqual(len(w.calls), 4)
        self.assertTrue(w.lines("ERROR", "start did not take"))

    def test_a_stop_that_fails_is_retried_too(self):
        w = self.night(38)
        w.fail = 1
        w.humidity(43)
        self.assertEqual(w.calls[2:], [TURN_OFF])
        w.tick(bh.RETRY_S)
        self.assertEqual(w.calls[3:], [TURN_OFF])
        self.assertEqual(w.device["state"], "off")


class RestartSurvival(Base):
    def test_the_state_is_written_when_it_changes(self):
        w = self.night(38)
        saved = w.saved()
        self.assertEqual((saved["asleep"], saved["wick"]), (True, "wet"))
        self.assertEqual(saved["run"]["kind"], "humidify")

    def test_a_restart_in_the_night_still_dries_in_the_morning(self):
        w = self.night(38)
        w.start()
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE], "no command at start-up, the run goes on")
        w.tick(5 * H)
        w.sleep(False)
        self.assertEqual(w.calls[2:], [DRYING_MODE])

    def test_a_restart_across_the_morning_edge_dries_at_start_up(self):
        w = self.night(38)
        w.tick(5 * H)
        w.states[SLEEP] = "off"
        w.start()
        self.assertEqual(w.calls[2:], [DRYING_MODE])

    def test_a_restart_during_the_drying_keeps_its_deadline(self):
        w = self.night(38)
        w.tick(5 * H)
        w.sleep(False)
        w.tick(H)
        w.start()
        self.assertEqual(w.calls[2:], [DRYING_MODE], "no second drying command")
        w.tick(H - 10)
        self.assertNotIn(TURN_OFF, w.calls)
        w.tick(10)
        self.assertEqual(w.calls[3:], [TURN_OFF])

    def test_the_deadline_passing_while_the_app_is_down_turns_it_off_at_start_up(self):
        w = self.night(38)
        w.tick(5 * H)
        w.sleep(False)
        w.clock += 3 * H
        w.start()
        self.assertEqual(w.calls[3:], [TURN_OFF])

    def test_a_timer_is_set_for_a_deadline_that_is_still_ahead(self):
        w = self.night(38)
        w.tick(5 * H)
        w.sleep(False)
        w.tick(H)
        w.timers.clear()
        w.start()
        self.assertIn(("_tick", H + 5), w.timers)

    def test_a_first_start_takes_the_current_sleep_mode_as_no_edge(self):
        w = self.world()
        w.states[SLEEP] = "on"
        w.humidity(45)
        w.start()
        self.assertEqual(w.calls, [])
        w.tick(8 * H)
        w.sleep(False)
        self.assertEqual(w.calls, [], "no run, no drying")

    def test_a_missing_or_broken_state_file_starts_empty(self):
        for content in (None, "", "{", "[]", '{"wick": "mouldy", "asleep": "yes", "run": 4}'):
            with self.subTest(content=content):
                w = self.world()
                if content is not None:
                    w.state_file.write_text(content)
                w.start()
                self.assertEqual(w.app._wick, "dry")
                self.assertIsNone(w.app._run)

    def test_a_drying_wick_without_a_deadline_is_read_as_wet(self):
        w = self.world()
        w.state_file.write_text(json.dumps({"asleep": False, "wick": "drying"}))
        w.start()
        self.assertEqual(w.app._wick, "wet")

    def test_a_state_file_that_cannot_be_written_is_logged_not_raised(self):
        w = self.world(args={"state_file": "/proc/nonexistent/state.json"})
        w.humidity(38)
        w.sleep(True)
        self.assertTrue(w.lines("WARNING", "state save failed"))
        self.assertEqual(w.device["state"], "on")


class RunLog(Base):
    def test_a_night_run_is_logged_with_times_humidity_and_minutes(self):
        w = self.world()
        w.tick(H)
        w.humidity(37.0)
        w.sleep(True)
        w.tick(90 * MIN)
        self.assertEqual(w.run_log(), [], "nothing is logged while it runs")
        w.humidity(42.0)
        (record,) = w.run_log()
        self.assertEqual(record, {
            "night": "2026-11-24",
            "kind": "humidify",
            "start": "2026-11-24T23:00:00+01:00",
            "stop": "2026-11-25T00:30:00+01:00",
            "minutes": 90.0,
            "humidity_start": 37.0,
            "humidity_source_start": PRIMARY,
            "humidity_stop": 42.0,
            "humidity_source_stop": PRIMARY,
            "stop_reason": "Humidity 42 % is at or above 42 %",
        })

    def test_every_run_of_the_night_is_a_line_with_the_same_night(self):
        w = self.night(38)
        w.tick(H)
        w.humidity(43)
        w.tick(H)
        w.humidity(38)
        w.tick(H)
        w.humidity(43)
        runs = w.run_log()
        self.assertEqual(len(runs), 2)
        self.assertEqual({r["night"] for r in runs}, {"2026-11-24"})
        self.assertEqual([r["minutes"] for r in runs], [60.0, 60.0])

    def test_the_stop_reason_names_a_window(self):
        w = self.night(38)
        w.tick(20 * MIN)
        w.window(True)
        self.assertEqual(w.run_log()[0]["stop_reason"], "Window open")
        self.assertEqual(w.run_log()[0]["minutes"], 20.0)

    def test_the_drying_is_a_line_of_kind_dry_on_the_same_night(self):
        w = self.night(38)
        w.tick(8 * H)
        w.sleep(False)
        w.tick(2 * H)
        runs = w.run_log()
        self.assertEqual([r["kind"] for r in runs], ["humidify", "dry"])
        self.assertEqual(runs[0]["stop_reason"], "Sleep mode off")
        self.assertEqual(runs[1]["night"], runs[0]["night"])
        self.assertEqual(runs[1]["stop_reason"], "Drying time over")
        self.assertEqual(runs[1]["minutes"], 120.0)

    def test_a_run_open_across_a_restart_keeps_its_start(self):
        w = self.night(37)
        w.start()
        w.tick(H)
        w.humidity(43)
        (record,) = w.run_log()
        self.assertEqual(record["start"], "2026-11-24T22:00:00+01:00")
        self.assertEqual(record["humidity_start"], 37)
        self.assertEqual(record["minutes"], 60.0)

    def test_a_run_that_ended_by_hand_says_so(self):
        w = self.night(38)
        w.tick(10 * MIN)
        w.by_hand("off", "sleep")
        self.assertEqual(w.run_log()[0]["stop_reason"], "Turned off")

    def test_a_log_that_cannot_be_written_does_not_stop_the_app(self):
        w = self.world(args={"log_file": "/proc/nonexistent/run_log.jsonl"})
        w.humidity(38)
        w.sleep(True)
        w.humidity(43)
        self.assertTrue(w.lines("WARNING", "run log write failed"))
        self.assertEqual(w.device["state"], "off")


class PublishedSensor(Base):
    def test_every_state_has_a_reason(self):
        seen = {}
        w = self.world()
        seen["off"] = w.sensor()
        w.humidity(38)
        w.sleep(True)
        seen["humidifying"] = w.sensor()
        w.tick(5 * H)
        w.sleep(False)
        seen["drying"] = w.sensor()
        w.by_hand("on", "high")
        seen["manual"] = w.sensor()
        w.tank("empty")
        seen["no_water"] = w.sensor()
        w.tank("high")
        w.device["state"] = "unavailable"
        w.tick(5 * MIN)
        seen["unknown"] = w.sensor()
        for state, (published, attrs) in seen.items():
            with self.subTest(state=state):
                self.assertEqual(published, state)
                self.assertTrue(attrs["reason"])
                self.assertTrue(attrs["icon"].startswith("mdi:"))
                self.assertEqual(attrs["friendly_name"], "Bedroom humidifier")

    def test_it_replaces_the_attributes_and_goes_to_the_configured_entity(self):
        w = self.world()
        call = w.app.set_state.call_args_list[-1]
        self.assertEqual(call.args, (HEALTH,))
        self.assertIs(call.kwargs["replace"], True)

    def test_attributes_carry_the_control_reading_the_level_and_the_mode(self):
        w = self.night(38.5)
        state, attrs = w.sensor()
        self.assertEqual(state, "humidifying")
        self.assertEqual((attrs["humidity"], attrs["water_level"], attrs["mode"]), (38.5, "high", "sleep"))
        self.assertEqual(attrs["humidity_source"], PRIMARY)
        self.assertIn("38.5", attrs["reason"])

    def test_it_is_republished_at_every_check_and_not_on_an_event_that_changes_nothing(self):
        w = self.world()
        n = w.app.set_state.call_count
        w.person("home")
        w.person("home")
        self.assertEqual(w.app.set_state.call_count, n)
        w.tick(5 * MIN)
        self.assertEqual(w.app.set_state.call_count, n + 1)
        w.tick(5 * MIN)
        self.assertEqual(w.app.set_state.call_count, n + 2)

    def test_a_failed_publish_is_logged_and_tried_again(self):
        w = self.world()
        w.app.set_state.side_effect = RuntimeError("down")
        w.humidity(30)
        self.assertTrue(w.lines("WARNING", "publish failed"))
        w.app.set_state.side_effect = None
        n = w.app.set_state.call_count
        w.tick(MIN)
        self.assertEqual(w.app.set_state.call_count, n + 1)

    def test_a_state_change_is_logged_with_its_reason(self):
        w = self.night(38)
        self.assertTrue(w.lines("INFO", "off -> humidifying: Humidity 38 % is below 40 %"))


class HumiditySources(Base):
    """The bedroom humidity is the first listed source that reads a number and reported within 30 minutes."""

    def source(self, w):
        return w.sensor()[1].get("humidity_source")

    def test_the_first_source_is_used_when_it_is_fresh_and_nothing_is_averaged(self):
        w = self.world(**{PRIMARY: "38.0", FALLBACK: "45.0"})
        w.sleep(True)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE], "38 % from the first source, not 45 % or their mean")
        _, attrs = w.sensor()
        self.assertEqual((attrs["humidity"], attrs["humidity_source"]), (38.0, PRIMARY))
        self.assertIn("Humidity 38 %", attrs["reason"])

    def test_the_second_source_is_used_when_the_first_has_gone_quiet(self):
        w = self.world(**{PRIMARY: "38.0", FALLBACK: "45.0"})
        w.quiet(PRIMARY, 31 * MIN)
        w.sleep(True)
        self.assertEqual(w.calls, [], "45 % from the second source; the old 38 % is not used")
        _, attrs = w.sensor()
        self.assertEqual((attrs["humidity"], attrs["humidity_source"]), (45.0, FALLBACK))

    def test_a_report_exactly_thirty_minutes_old_counts_and_one_second_older_does_not(self):
        for age, used in ((30 * MIN, PRIMARY), (30 * MIN + 1, FALLBACK)):
            with self.subTest(age=age):
                w = self.world(**{PRIMARY: "38.0", FALLBACK: "45.0"})
                w.quiet(PRIMARY, age)
                w.sleep(True)
                self.assertEqual(self.source(w), used)

    def test_a_first_source_that_ages_out_hands_over_at_the_next_check(self):
        w = self.world(**{PRIMARY: "38.0", FALLBACK: "45.0"})
        w.sleep(True)
        w.quiet(PRIMARY)
        w.tick_for(30 * MIN)
        self.assertEqual(self.source(w), PRIMARY)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])
        w.fallback(45.0)
        w.tick(MIN)
        self.assertEqual(self.source(w), FALLBACK)
        self.assertEqual(w.calls[2:], [TURN_OFF])

    def test_the_second_source_is_used_when_the_first_does_not_read_a_number(self):
        for bad in ("unavailable", "unknown", "", "abc", "nan"):
            with self.subTest(bad=bad):
                w = self.world(**{PRIMARY: bad, FALLBACK: "38.0"})
                w.sleep(True)
                self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])
                self.assertEqual(self.source(w), FALLBACK)

    def test_the_second_source_is_used_when_the_first_does_not_exist(self):
        w = self.world(**{FALLBACK: "38.0"})
        del w.states[PRIMARY]
        w.sleep(True)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])
        self.assertEqual(self.source(w), FALLBACK)

    def test_the_first_source_is_used_again_as_soon_as_it_reports(self):
        w = self.world(**{PRIMARY: "38.0", FALLBACK: "45.0"})
        w.quiet(PRIMARY, 31 * MIN)
        w.sleep(True)
        self.assertEqual(self.source(w), FALLBACK)
        w.humidity(38.0)
        self.assertEqual(self.source(w), PRIMARY)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])

    def test_an_event_on_the_second_source_is_acted_on_while_the_first_is_quiet(self):
        w = self.world(**{PRIMARY: "45.0", FALLBACK: "45.0"})
        w.quiet(PRIMARY, 31 * MIN)
        w.sleep(True)
        self.assertEqual(w.calls, [])
        w.fallback(39.9)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE])

    def test_no_fresh_source_does_not_start_a_stopped_humidifier(self):
        w = self.world(**{PRIMARY: "35.0", FALLBACK: "35.0"})
        w.quiet(PRIMARY, 31 * MIN)
        w.quiet(FALLBACK, 31 * MIN)
        w.sleep(True)
        w.tick_for(2 * H)
        self.assertEqual(w.calls, [])
        state, attrs = w.sensor()
        self.assertEqual(state, "off")
        self.assertIn("unreadable", attrs["reason"])
        self.assertNotIn("humidity", attrs)
        self.assertNotIn("humidity_source", attrs)
        self.assertEqual(len(w.lines("WARNING", "bedroom humidity unreadable")), 1)

    def test_no_fresh_source_leaves_a_running_humidifier_as_it_is(self):
        w = self.night(38)
        w.fallback(45)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE], "the second source does not count while the first is fresh")
        w.quiet(PRIMARY)
        w.quiet(FALLBACK)
        w.tick_for(31 * MIN)
        self.assertEqual(w.calls, [TURN_ON, SLEEP_MODE], "no stop on a 45 % that is 31 minutes old")
        self.assertEqual(w.device["state"], "on")
        self.assertEqual(len(w.lines("WARNING", "bedroom humidity unreadable")), 1)
        w.fallback(45)
        self.assertEqual(w.calls[2:], [TURN_OFF])
        self.assertTrue(w.lines("INFO", "readable again"))

    def test_the_stamp_is_last_reported_and_then_last_updated(self):
        cases = (
            ("repeated value, reported a minute ago", MIN, 2 * H, PRIMARY),
            ("reported 31 minutes ago", 31 * MIN, 31 * MIN, FALLBACK),
            ("no last_reported, updated 5 minutes ago", None, 5 * MIN, PRIMARY),
            ("no last_reported, updated 31 minutes ago", None, 31 * MIN, FALLBACK),
            ("no stamps at all", None, None, FALLBACK),
            ("unparseable stamps", "yesterday", "never", FALLBACK),
        )
        for name, reported, updated, used in cases:
            with self.subTest(name):
                w = self.world(**{PRIMARY: "38.0", FALLBACK: "45.0"})
                w.stamps[PRIMARY] = tuple(x if x is None or isinstance(x, str) else w.clock - x for x in (reported, updated))
                w.sleep(True)
                self.assertEqual(self.source(w), used)

    def test_the_humidity_is_rounded_to_a_tenth_of_a_percent(self):
        w = self.world(**{PRIMARY: "unavailable", FALLBACK: "30.0300006866455"})
        w.tick()
        self.assertEqual(w.sensor()[1]["humidity"], 30.0)
        w.fallback("41.949999")
        self.assertEqual(w.sensor()[1]["humidity"], 41.9)

    def test_the_sensor_names_the_source_and_changes_it_with_the_source(self):
        w = self.world(**{PRIMARY: "36.0", FALLBACK: "36.5"})
        self.assertEqual(self.source(w), PRIMARY)
        w.quiet(PRIMARY, 31 * MIN)
        w.tick()
        self.assertEqual(self.source(w), FALLBACK)
        self.assertEqual(w.sensor()[1]["humidity"], 36.5)

    def test_the_run_log_names_the_source_at_the_start_and_at_the_stop(self):
        w = self.world(**{PRIMARY: "38.0"})
        w.sleep(True)
        w.tick(H)
        w.quiet(PRIMARY, 31 * MIN)
        w.fallback(43)
        (record,) = w.run_log()
        self.assertEqual((record["humidity_start"], record["humidity_source_start"]), (38.0, PRIMARY))
        self.assertEqual((record["humidity_stop"], record["humidity_source_stop"]), (43.0, FALLBACK))

    def test_a_run_open_across_a_restart_keeps_the_source_it_started_on(self):
        w = self.world(**{PRIMARY: "38.0"})
        w.sleep(True)
        w.start()
        w.humidity(43)
        (record,) = w.run_log()
        self.assertEqual((record["humidity_source_start"], record["humidity_source_stop"]), (PRIMARY, PRIMARY))

    def test_a_run_saved_without_a_source_still_closes(self):
        w = self.night(38)
        saved = w.saved()
        del saved["run"]["source"]
        w.state_file.write_text(json.dumps(saved))
        w.start()
        w.humidity(43)
        (record,) = w.run_log()
        self.assertEqual((record["humidity_source_start"], record["humidity_source_stop"]), (None, PRIMARY))


class Configuration(unittest.TestCase):
    def test_the_yaml_names_the_entities_and_thresholds_of_the_assignment(self):
        self.assertEqual(YAML_ARGS["module"], "bedroom_humidifier")
        self.assertEqual(YAML_ARGS["class"], "BedroomHumidifier")
        self.assertEqual(YAML_ARGS["dependencies"], "MobileNotifier")
        self.assertEqual(YAML_ARGS["humidifier"], HUMIDIFIER)
        self.assertEqual(YAML_ARGS["water_level"], WATER)
        self.assertEqual(YAML_ARGS["humidity_sources"], [PRIMARY, FALLBACK])
        self.assertEqual(YAML_ARGS["humidity_max_age_min"], 240)
        self.assertNotIn("humidity", YAML_ARGS)
        self.assertEqual(YAML_ARGS["sleep_mode"], SLEEP)
        self.assertEqual(YAML_ARGS["window"], WINDOW)
        self.assertEqual(YAML_ARGS["health_entity"], HEALTH)
        self.assertEqual(YAML_ARGS["notify_target"], "mikkel")
        self.assertEqual((YAML_ARGS["on_below"], YAML_ARGS["off_at"]), (40, 42))
        self.assertEqual((YAML_ARGS["run_mode"], YAML_ARGS["drying_mode"], YAML_ARGS["drying_hours"]), ("sleep", "drying_filter", 2))

    def test_the_yaml_declares_no_log_stream_and_no_dictionary_values(self):
        self.assertNotIn("log", YAML_ARGS)
        for key, value in YAML_ARGS.items():
            self.assertNotIsInstance(value, dict, key)

    def test_the_state_file_lives_beside_the_app_on_the_box(self):
        self.assertEqual(YAML_ARGS["state_file"], "/conf/apps/climate/bedroom_humidifier_state.json")

    def test_every_arg_the_app_requires_is_in_the_yaml(self):
        source = (Path(bh.__file__)).read_text()
        for key in ("humidifier", "water_level", "humidity_sources", "sleep_mode", "window", "person", "health_entity"):
            self.assertIn(f'a["{key}"]', source)
            self.assertIn(key, YAML_ARGS)

    def test_thresholds_must_leave_a_band(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for args in ({"on_below": 42, "off_at": 42}, {"on_below": 43, "off_at": 42}, {"drying_hours": 0}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                World(tmp, args=args)

    def test_the_humidity_sources_must_be_a_list_of_entity_ids_and_the_age_positive(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for args in (
            {"humidity_sources": []},
            {"humidity_sources": PRIMARY},
            {"humidity_sources": [PRIMARY, 3]},
            {"humidity_max_age_min": 0},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                World(tmp, args=args)

    def test_the_age_limit_is_an_option_in_minutes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        w = World(tmp, args={"humidity_max_age_min": 90}, **{PRIMARY: "38.0", FALLBACK: "45.0"})
        w.quiet(PRIMARY, 89 * MIN)
        w.tick()
        self.assertEqual(w.sensor()[1]["humidity_source"], PRIMARY)
        w.quiet(PRIMARY, 91 * MIN)
        w.tick()
        self.assertEqual(w.sensor()[1]["humidity_source"], FALLBACK)

    def test_the_init_line_names_the_sources_and_the_age_limit(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        (line,) = World(tmp).lines("INFO", "initialized")
        self.assertIn(f"humidity from {PRIMARY}, {FALLBACK} (at most 30 min old)", line)

    def test_the_log_and_state_files_default_beside_the_module(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        w = World(tmp)
        args = {k: v for k, v in YAML_ARGS.items() if k != "state_file"}
        app = bh.BedroomHumidifier.__new__(bh.BedroomHumidifier)
        app.args = args
        app._now = lambda: T0
        app.get_state = w._get_state
        app.set_state = MagicMock()
        app.listen_state = MagicMock()
        app.run_every = MagicMock()
        app.run_in = MagicMock()
        app.log = lambda *a, **k: None
        app.get_app = MagicMock()
        app._persist = lambda: None
        app.initialize()
        here = Path(bh.__file__).resolve().parent
        self.assertEqual(app.state_file, here / "bedroom_humidifier_state.json")
        self.assertEqual(app.log_file, here / "bedroom_humidifier_log.jsonl")

    def test_the_listeners_cover_every_input_and_the_humidifier_attributes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        w = World(tmp)
        listened = {c.args[1]: c.kwargs for c in w.app.listen_state.call_args_list}
        self.assertEqual(set(listened), {SLEEP, WINDOW, PRIMARY, FALLBACK, WATER, PERSON, HUMIDIFIER})
        self.assertNotIn("sensor.bedroom_humidity", listened)
        self.assertEqual(listened[HUMIDIFIER], {"attribute": "all"})
        w.app.run_every.assert_called_once_with(w.app._tick, "now+60", 300)

    def test_log_lines_are_ascii(self):
        w = World(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(Path(w.state_file).parent), True)
        w.humidity(38)
        w.sleep(True)
        w.window(True)
        w.window(False)
        w.tank("empty")
        w.humidity(43)
        w.sleep(False)
        for _, line in w.logs:
            self.assertTrue(line.isascii(), line)


if __name__ == "__main__":
    unittest.main()
