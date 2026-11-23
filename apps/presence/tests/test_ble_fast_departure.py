import json
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

from ble_fast_departure import BleFastDeparture


DOOR = "binary_sensor.door"
AREA = "sensor.area"
WIFI = "device_tracker.phone"
SWITCH = "input_boolean.shadow"
TRANSMITTER = "sensor.transmitter"
START = datetime(2026, 11, 23, tzinfo=timezone.utc)


class FakeHouse:
    def __init__(self, log_file, states=None, transmitter=True):
        self.clock = START
        self.states = {}
        for entity, state in {DOOR: "off", AREA: "Kitchen", WIFI: "home", SWITCH: "on",
                              TRANSMITTER: "Transmitting", **(states or {})}.items():
            self.cache(entity, state)
        self.listeners = {}
        self.timers = {}
        self.next_timer = 0
        self.log_file = log_file
        self.app = BleFastDeparture.__new__(BleFastDeparture)
        self.app.args = {
            "resident": "Mikkel" if transmitter else "Kristine",
            "door": DOOR, "area_sensor": AREA, "tracker": WIFI, "shadow_switch": SWITCH,
            "log_file": str(log_file),
        }
        if transmitter:
            self.app.args["transmitter"] = TRANSMITTER
        self.app.get_now = lambda: self.clock
        self.app.get_state = self.get_state
        self.app.log = lambda *args, **kwargs: None
        self.app.listen_state = lambda callback, entity: self.listeners.update({entity: callback})
        self.app.run_in = self.run_in
        self.app.cancel_timer = lambda handle, silent: self.timers.pop(handle, None)
        self.app.initialize()

    def get_state(self, entity, attribute=None):
        state = self.states[entity]
        return state if attribute == "all" else state["state"]

    def cache(self, entity, state):
        self.states[entity] = {"state": state, "last_changed": self.clock.isoformat()}

    def run_in(self, callback, delay, **kwargs):
        self.next_timer += 1
        self.timers[self.next_timer] = (self.clock + timedelta(seconds=delay), callback, kwargs)
        return self.next_timer

    def advance(self, seconds):
        target = START + timedelta(seconds=seconds)
        while self.timers:
            handle, (due, callback, kwargs) = min(self.timers.items(), key=lambda item: item[1][0])
            if due > target:
                break
            self.timers.pop(handle)
            self.clock = due
            callback(kwargs)
        self.clock = target

    def change(self, seconds, entity, new):
        self.advance(seconds)
        old = self.get_state(entity)
        if old != new:
            self.cache(entity, new)
        self.listeners[entity](entity, "state", old, new, {})

    def read_log(self):
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]


class TestBleFastDeparture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log_file = Path(temporary.name) / "departure.jsonl"

    def house(self, states=None, transmitter=True):
        self.log_file.unlink(missing_ok=True)
        return FakeHouse(self.log_file, states, transmitter)

    def test_accepts_window_boundary_and_logs_one_wifi_confirmation(self):
        house = self.house()
        house.change(10, DOOR, "on")
        house.change(20, DOOR, "off")
        house.change(310, AREA, "unknown")
        house.advance(429.9)
        self.assertEqual(house.read_log(), [])
        _, callback, kwargs = next(iter(house.timers.values()))
        house.advance(430)
        callback(kwargs)
        house.change(510, WIFI, "not_home")
        house.change(511, WIFI, "not_home")
        house.advance(2000)
        candidate, confirmed = house.read_log()
        self.assertEqual((candidate["event"], confirmed["event"]), ("candidate", "wifi_confirmed"))
        self.assertEqual(candidate["candidate_at"], confirmed["candidate_at"])
        self.assertEqual(confirmed["wifi_lead_s"], 80)

    def test_expired_opening_and_startup_with_door_already_open_do_not_arm(self):
        house = self.house()
        house.change(10, DOOR, "on")
        house.change(310.1, AREA, "unknown")
        house.advance(900)
        self.assertEqual(house.read_log(), [])

        house = self.house({DOOR: "on"})
        house.change(10, AREA, "unknown")
        house.advance(900)
        self.assertEqual(house.read_log(), [])

    def test_unavailable_door_bouncing_open_and_bluetooth_already_silent_do_not_arm(self):
        house = self.house({DOOR: "unavailable"})
        house.change(10, DOOR, "on")
        house.change(20, AREA, "unknown")
        house.change(25, DOOR, "off")
        house.change(30, DOOR, "on")
        house.advance(900)
        self.assertEqual(house.read_log(), [])

    def test_116_second_recovery_cancels_but_a_later_gap_can_use_the_same_opening(self):
        house = self.house(transmitter=False)
        house.change(10, DOOR, "on")
        house.change(15, DOOR, "off")
        house.change(20, AREA, "unknown")
        _, callback, stale = next(iter(house.timers.values()))
        house.change(136, AREA, "Kitchen")
        house.change(137, AREA, "unknown")
        house.advance(140)
        callback(stale)
        house.advance(256)
        self.assertEqual(house.read_log(), [])
        house.advance(257)
        house.change(260, AREA, "Kitchen")
        rows = house.read_log()
        self.assertEqual([row["event"] for row in rows], ["candidate", "ble_recovered_while_wifi_home"])
        self.assertEqual(rows[0]["ble_unheard_at"], (START + timedelta(seconds=137)).isoformat())

    def test_cannot_arm_when_wifi_is_away_shadow_is_disabled_or_transmitter_is_off(self):
        for entity, state in [(WIFI, "not_home"), (SWITCH, None), (TRANSMITTER, "Stopped")]:
            with self.subTest(entity=entity):
                house = self.house({entity: state})
                house.change(10, DOOR, "on")
                house.change(20, AREA, "unknown")
                house.advance(900)
                self.assertEqual(house.read_log(), [])

    def test_unstable_sources_disarm_even_if_they_recover_before_deadline(self):
        for entity, unstable, recovered in [(WIFI, "unavailable", "home"), (TRANSMITTER, "Stopped", "Transmitting"),
                                             (SWITCH, "off", "on"), (DOOR, "unknown", "off"),
                                             (AREA, "unavailable", "unknown")]:
            with self.subTest(entity=entity):
                house = self.house()
                house.change(10, DOOR, "on")
                house.change(20, AREA, "unknown")
                house.change(30, entity, unstable)
                house.change(40, entity, recovered)
                house.advance(900)
                self.assertEqual(house.read_log(), [])

    def test_silence_timer_rechecks_source_stability_before_queued_callbacks_run(self):
        for entity, state in [(WIFI, "home"), (AREA, "unknown"), (TRANSMITTER, "Transmitting"), (SWITCH, "on")]:
            with self.subTest(entity=entity):
                house = self.house()
                house.change(10, DOOR, "on")
                house.change(20, AREA, "unknown")
                house.advance(139)
                house.cache(entity, state)
                house.advance(140)
                self.assertEqual(house.read_log(), [])

    def test_review_resolves_wifi_unavailable_and_wifi_timeout_without_confirmation(self):
        for wifi, expected in [("unknown", "wifi_unavailable"), ("home", "wifi_not_confirmed_within_window")]:
            with self.subTest(wifi=wifi):
                house = self.house()
                house.change(10, DOOR, "on")
                house.change(20, AREA, "unknown")
                house.advance(140)
                house.change(160, WIFI, wifi)
                house.advance(1500)
                self.assertEqual([row["event"] for row in house.read_log()], ["candidate", expected])
