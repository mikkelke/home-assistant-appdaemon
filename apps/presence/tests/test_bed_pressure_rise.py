# tests/test_bed_pressure_rise.py - BedPressureRise: baseline learning from an empty room, the
# rise/hold/release rule per side, unreadable pressure, publishing, and the shipped yaml wiring
# of the published entity into the apps that read it.
# Same __new__ + stubbed-primitives harness as the other tests in this directory.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import bed_pressure_rise as bpr  # noqa: E402
import bedroom_evidence  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

APPS = Path(__file__).resolve().parents[2]

OUTPUT = "binary_sensor.bed_pressure_rise"
LEFT_P = "sensor.bed_presence_6b9c94_left_pressure"
RIGHT_P = "sensor.bed_presence_6b9c94_right_pressure"
LEFT_OCC = "binary_sensor.bed_presence_6b9c94_bed_occupied_left"
RIGHT_OCC = "binary_sensor.bed_presence_6b9c94_bed_occupied_right"
ROOM = "binary_sensor.bedroom_presence_presence"
PIR = "binary_sensor.bedroom_presence_pir_detection"
QUIET = (ROOM, PIR, LEFT_OCC, RIGHT_OCC)

T0 = 1_800_000_000.0

NUMERIC = ("rise_points", "release_points", "hold_sec", "empty_room_sec")
BASE_ARGS = {
    "left_pressure": LEFT_P,
    "right_pressure": RIGHT_P,
    "left_occupied": LEFT_OCC,
    "right_occupied": RIGHT_OCC,
    "room_witnesses": [ROOM, PIR],
    "output": OUTPUT,
    "rise_points": 40,
    "release_points": 32,
    "hold_sec": 20,
    "empty_room_sec": 300,
}

UNREADABLE = ("unknown", "unavailable", "abc", "nan", None)


class Rig:
    """BedPressureRise with initialize() run against stubbed AppDaemon primitives. The room
    starts empty (every witness "off") with the bed empty at 13 % on the left and 5 % on the
    right, so learn() takes the baselines 300 s in and the levels become: left on at 53,
    off below 45; right on at 45, off below 37."""

    def __init__(self, states=None, args=None):
        self.now = T0
        self.states = {entity: "off" for entity in QUIET}
        self.states.update({LEFT_P: "13", RIGHT_P: "5"})
        self.states.update(states or {})
        self.published = []
        self.logs = []
        self.fail_publish = False

        app = bpr.BedPressureRise.__new__(bpr.BedPressureRise)
        app.args = dict(BASE_ARGS if args is None else args)
        app.get_state = lambda entity, attribute=None, **kw: self.states.get(entity)
        app.set_state = self._set_state
        app.listen_state = MagicMock()
        app.run_every = MagicMock()
        app.log = lambda message, **kw: self.logs.append(message)
        app._now = lambda: self.now
        app.initialize()
        self.app = app

    def _set_state(self, entity, **kwargs):
        if self.fail_publish:
            raise RuntimeError("HA unreachable")
        self.published.append((entity, kwargs))

    def put(self, entity, value):
        self.states[entity] = value

    def at(self, seconds):
        """Evaluate `seconds` after T0 and return the (state, attributes) it computed."""
        self.now = T0 + seconds
        return self.app._evaluate(self.now)

    def learn(self):
        self.at(0)
        return self.at(300)

    def enter(self, seconds):
        """Someone is in the room: the room witness reads on, which freezes the baselines."""
        self.put(ROOM, "on")
        return self.at(seconds)

    def sit_up(self, left="54"):
        """Baselines learned, someone in the room, the left side now at `left` and its rise
        counted from 310 s."""
        self.learn()
        self.enter(301)
        self.put(LEFT_P, left)
        self.at(310)

    def log_lines(self, prefix):
        return [m for m in self.logs if m.startswith(prefix)]


class Baseline(unittest.TestCase):
    def test_learned_only_after_a_full_empty_run(self):
        rig = Rig()
        for seconds in (0, 150, 299):
            state, attrs = rig.at(seconds)
            self.assertEqual(state, "off")
            self.assertIsNone(attrs["left_baseline"])
            self.assertIsNone(attrs["right_baseline"])
            self.assertIsNone(attrs["baseline_learned_at"])
            self.assertEqual(attrs["reason"], "no baseline yet")
        state, attrs = rig.at(300)
        self.assertEqual(state, "off")
        self.assertEqual(attrs["left_baseline"], 13.0)
        self.assertEqual(attrs["right_baseline"], 5.0)
        self.assertEqual(attrs["baseline_learned_at"], "2027-01-15T08:05:00+00:00")
        self.assertEqual(attrs["reason"], "room empty 5 min: baseline learned")

    def test_any_state_but_off_restarts_the_empty_run(self):
        for entity in QUIET:
            for value in ("on", "unknown", "unavailable", None):
                with self.subTest(entity=entity, value=value):
                    rig = Rig()
                    rig.at(200)
                    rig.put(entity, value)
                    rig.at(210)
                    rig.put(entity, "off")
                    rig.at(220)
                    self.assertIsNone(rig.at(519)[1]["left_baseline"])
                    self.assertEqual(rig.at(520)[1]["left_baseline"], 13.0)

    def test_a_witness_that_stays_unreadable_blocks_learning(self):
        for value in ("unknown", "unavailable", None):
            with self.subTest(value=value):
                rig = Rig({PIR: value})
                for seconds in (0, 300, 3600):
                    self.assertIsNone(rig.at(seconds)[1]["left_baseline"])

    def test_follows_the_empty_level_and_freezes_once_the_run_breaks(self):
        rig = Rig()
        rig.learn()
        rig.put(LEFT_P, "20")
        self.assertEqual(rig.at(305)[1]["left_baseline"], 20.0)
        rig.put(LEFT_P, "7.34")
        self.assertEqual(rig.at(310)[1]["left_baseline"], 7.3)
        rig.enter(311)
        rig.put(LEFT_P, "30")
        _, attrs = rig.at(400)
        self.assertEqual(attrs["left_baseline"], 7.3)
        self.assertEqual(attrs["baseline_learned_at"], "2027-01-15T08:05:10+00:00")

    def test_the_unrounded_baseline_decides(self):
        rig = Rig({LEFT_P: "13.25"})
        rig.learn()
        rig.enter(301)
        rig.put(LEFT_P, "53.22")
        rig.at(310)
        self.assertEqual(rig.at(330)[0], "off")
        rig.put(LEFT_P, "53.25")
        rig.at(331)
        self.assertEqual(rig.at(351)[0], "on")


class Rise(unittest.TestCase):
    def test_rise_held_less_than_hold_sec_does_not_fire(self):
        rig = Rig()
        rig.sit_up()
        state, attrs = rig.at(329)
        self.assertEqual(state, "off")
        self.assertEqual(attrs["side"], "none")
        self.assertEqual(attrs["reason"], "left 54 >= baseline 13 + 40 for 19 of 20 s")

    def test_rise_held_for_hold_sec_fires(self):
        rig = Rig()
        rig.sit_up()
        state, attrs = rig.at(330)
        self.assertEqual(state, "on")
        self.assertEqual(attrs["side"], "left")
        self.assertEqual(attrs["reason"], "left 54 >= baseline 13 + 40 for 20 s")

    def test_threshold_is_baseline_plus_rise_points_inclusive(self):
        for pressure, expected in (("52.9", "off"), ("53", "on"), ("53.1", "on")):
            with self.subTest(pressure=pressure):
                rig = Rig()
                rig.sit_up(pressure)
                self.assertEqual(rig.at(330)[0], expected)

    def test_dip_below_the_rise_level_restarts_the_hold(self):
        rig = Rig()
        rig.sit_up()
        rig.put(LEFT_P, "52.9")
        rig.at(325)
        rig.put(LEFT_P, "54")
        rig.at(326)
        self.assertEqual(rig.at(345)[0], "off")
        self.assertEqual(rig.at(346)[0], "on")

    def test_no_baseline_means_off_however_high_the_pressure(self):
        rig = Rig({LEFT_P: "90", ROOM: "on"})
        for seconds in (0, 20, 3600):
            state, attrs = rig.at(seconds)
            self.assertEqual(state, "off")
            self.assertEqual(attrs["reason"], "no baseline yet")

    def test_configured_numbers_drive_the_rule(self):
        args = dict(BASE_ARGS, rise_points=10, release_points=5, hold_sec=3, empty_room_sec=60)
        rig = Rig(args=args)
        self.assertIsNone(rig.at(59)[1]["left_baseline"])
        self.assertEqual(rig.at(60)[1]["reason"], "room empty 1 min: baseline learned")
        rig.enter(61)
        rig.put(LEFT_P, "23")
        rig.at(70)
        self.assertEqual(rig.at(72)[0], "off")
        self.assertEqual(rig.at(73)[0], "on")
        rig.put(LEFT_P, "18")
        self.assertEqual(rig.at(74)[0], "on")
        rig.put(LEFT_P, "17.9")
        self.assertEqual(rig.at(75)[0], "off")

    def test_numeric_keys_default_to_the_shipped_values(self):
        args = {k: v for k, v in BASE_ARGS.items() if k not in NUMERIC}
        app = Rig(args=args).app
        self.assertEqual(
            (app.rise_points, app.release_points, app.hold_sec, app.empty_room_sec),
            (40.0, 32.0, 20.0, 300.0),
        )


class Release(unittest.TestCase):
    def on_rig(self):
        rig = Rig()
        rig.sit_up()
        self.assertEqual(rig.at(330)[0], "on")
        return rig

    def test_stays_on_down_to_baseline_plus_release_points(self):
        rig = self.on_rig()
        rig.put(LEFT_P, "45")
        self.assertEqual(rig.at(340)[0], "on")

    def test_turns_off_at_once_below_baseline_plus_release_points(self):
        rig = self.on_rig()
        rig.put(LEFT_P, "44.9")
        state, attrs = rig.at(331)
        self.assertEqual(state, "off")
        self.assertEqual(attrs["side"], "none")
        self.assertEqual(attrs["reason"], "left 44.9 < baseline 13 + 32")

    def test_needs_a_new_full_hold_after_release(self):
        rig = self.on_rig()
        rig.put(LEFT_P, "30")
        rig.at(331)
        rig.put(LEFT_P, "54")
        rig.at(332)
        self.assertEqual(rig.at(351)[0], "off")
        self.assertEqual(rig.at(352)[0], "on")


class EmptyRoomReleases(unittest.TestCase):
    def test_stuck_on_side_is_released_once_the_room_has_been_empty_long_enough(self):
        rig = Rig()
        rig.sit_up()
        self.assertEqual(rig.at(330)[0], "on")
        rig.put(ROOM, "off")
        rig.at(340)
        self.assertEqual(rig.at(639)[0], "on")
        state, attrs = rig.at(640)
        self.assertEqual(state, "off")
        self.assertEqual(attrs["side"], "none")
        self.assertEqual(attrs["left_baseline"], 54.0)
        self.assertEqual(attrs["reason"], "room empty 5 min: baseline learned")
        self.assertEqual(rig.at(700)[0], "off")
        self.assertEqual(rig.log_lines("BedPressureRise off"),
                         ["BedPressureRise off: room empty 5 min: baseline learned"])

    def test_an_occupied_sensor_still_on_keeps_the_run_from_completing(self):
        rig = Rig()
        rig.sit_up()
        self.assertEqual(rig.at(330)[0], "on")
        rig.put(ROOM, "off")
        rig.put(LEFT_OCC, "on")
        for seconds in (340, 1000, 5000):
            self.assertEqual(rig.at(seconds)[0], "on")

class UnreadablePressure(unittest.TestCase):
    def test_counts_as_below_threshold_and_restarts_the_hold(self):
        for value in UNREADABLE:
            with self.subTest(value=value):
                rig = Rig()
                rig.sit_up()
                rig.put(LEFT_P, value)
                _, attrs = rig.at(320)
                self.assertEqual(attrs["reason"], "left pressure unreadable")
                self.assertIsNone(attrs["left_pressure"])
                rig.put(LEFT_P, "54")
                rig.at(321)
                self.assertEqual(rig.at(340)[0], "off")
                self.assertEqual(rig.at(341)[0], "on")

    def test_turns_an_on_side_off_at_once(self):
        for value in UNREADABLE:
            with self.subTest(value=value):
                rig = Rig()
                rig.sit_up()
                self.assertEqual(rig.at(330)[0], "on")
                rig.put(LEFT_P, value)
                state, attrs = rig.at(331)
                self.assertEqual(state, "off")
                self.assertEqual(attrs["side"], "none")
                self.assertEqual(attrs["reason"], "left pressure unreadable")

    def test_does_not_overwrite_a_learned_baseline(self):
        for value in UNREADABLE:
            with self.subTest(value=value):
                rig = Rig()
                rig.learn()
                rig.put(LEFT_P, value)
                rig.put(RIGHT_P, "9")
                _, attrs = rig.at(305)
                self.assertEqual(attrs["left_baseline"], 13.0)
                self.assertEqual(attrs["right_baseline"], 9.0)

    def test_never_learns_a_baseline_from_it(self):
        rig = Rig({LEFT_P: "unavailable"})
        _, attrs = rig.at(300)
        self.assertIsNone(attrs["left_baseline"])
        self.assertEqual(attrs["right_baseline"], 5.0)

    def test_nothing_readable_at_all_is_still_no_baseline_yet(self):
        rig = Rig({LEFT_P: "unknown", RIGHT_P: None})
        _, attrs = rig.at(300)
        self.assertEqual(attrs["reason"], "no baseline yet")
        self.assertIsNone(attrs["baseline_learned_at"])
        self.assertEqual(rig.log_lines("BedPressureRise baseline learned"), [])

    def test_one_unreadable_side_does_not_stop_the_other(self):
        rig = Rig()
        rig.learn()
        rig.enter(301)
        rig.put(LEFT_P, "unavailable")
        rig.put(RIGHT_P, "50")
        rig.at(310)
        state, attrs = rig.at(330)
        self.assertEqual((state, attrs["side"]), ("on", "right"))
        self.assertEqual(attrs["reason"], "left pressure unreadable; right 50 >= baseline 5 + 40 for 20 s")


class OutputAndAttributes(unittest.TestCase):
    def test_either_side_turns_the_output_on(self):
        rig = Rig()
        rig.learn()
        rig.enter(301)
        rig.put(RIGHT_P, "50")
        rig.at(310)
        state, attrs = rig.at(330)
        self.assertEqual((state, attrs["side"]), ("on", "right"))
        rig.put(LEFT_P, "60")
        rig.at(340)
        state, attrs = rig.at(360)
        self.assertEqual((state, attrs["side"]), ("on", "both"))
        self.assertEqual(attrs["reason"],
                         "left 60 >= baseline 13 + 40 for 20 s; right 50 >= baseline 5 + 40 for 20 s")
        rig.put(RIGHT_P, "36")
        state, attrs = rig.at(361)
        self.assertEqual((state, attrs["side"]), ("on", "left"))
        rig.put(LEFT_P, "10")
        state, attrs = rig.at(362)
        self.assertEqual((state, attrs["side"]), ("off", "none"))

    def test_the_sides_hold_and_release_independently(self):
        rig = Rig()
        rig.learn()
        rig.enter(301)
        rig.put(LEFT_P, "60")
        rig.at(310)
        rig.put(RIGHT_P, "50")
        rig.at(320)
        self.assertEqual(rig.at(330)[1]["side"], "left")
        self.assertEqual(rig.at(340)[1]["side"], "both")

    def test_attributes(self):
        rig = Rig()
        rig.learn()
        rig.enter(301)
        rig.put(LEFT_P, "53.66")
        rig.put(RIGHT_P, "6.04")
        rig.at(310)
        state, attrs = rig.at(330)
        self.assertEqual(state, "on")
        self.assertEqual(attrs, {
            "friendly_name": "Bed pressure rise",
            "device_class": "occupancy",
            "side": "left",
            "left_baseline": 13.0,
            "right_baseline": 5.0,
            "left_pressure": 53.7,
            "right_pressure": 6.0,
            "baseline_learned_at": "2027-01-15T08:05:00+00:00",
            "reason": "left 53.7 >= baseline 13 + 40 for 20 s",
        })

    def test_idle_reason_when_nothing_rises(self):
        rig = Rig()
        rig.learn()
        rig.enter(301)
        _, attrs = rig.at(310)
        self.assertEqual(attrs["reason"], "no rise over baseline")


class Publishing(unittest.TestCase):
    def test_publishes_the_output_entity_at_start(self):
        rig = Rig()
        self.assertEqual(len(rig.published), 1)
        entity, kwargs = rig.published[0]
        self.assertEqual(entity, OUTPUT)
        self.assertEqual(kwargs["state"], "off")
        self.assertIs(kwargs["replace"], True)
        self.assertEqual(kwargs["attributes"]["side"], "none")
        self.assertEqual(kwargs["attributes"]["reason"], "no baseline yet")

    def test_republishes_at_least_every_minute_and_not_in_between(self):
        rig = Rig()
        for seconds in range(5, 60, 5):
            rig.at(seconds)
        self.assertEqual(len(rig.published), 1)
        rig.at(60)
        self.assertEqual(len(rig.published), 2)
        rig.at(119)
        self.assertEqual(len(rig.published), 2)
        rig.at(120)
        self.assertEqual(len(rig.published), 3)

    def test_a_state_or_side_change_publishes_at_once_but_a_pressure_change_alone_does_not(self):
        rig = Rig()
        rig.learn()
        rig.enter(301)
        rig.put(LEFT_P, "54")
        rig.at(310)
        count = len(rig.published)
        rig.at(330)
        self.assertEqual(len(rig.published), count + 1)
        self.assertEqual(rig.published[-1][1]["state"], "on")
        rig.put(LEFT_P, "58")
        rig.at(335)
        self.assertEqual(len(rig.published), count + 1)
        rig.put(RIGHT_P, "50")
        rig.at(340)
        rig.at(360)
        self.assertEqual(len(rig.published), count + 2)
        self.assertEqual(rig.published[-1][1]["attributes"]["side"], "both")

    def test_a_clock_that_jumped_back_does_not_stall_the_republish(self):
        rig = Rig()
        rig.now = T0 - 3600
        rig.app._evaluate(rig.now)
        self.assertEqual(len(rig.published), 2)

    def test_a_failed_publish_is_retried_on_the_next_evaluation(self):
        rig = Rig()
        rig.sit_up()
        rig.fail_publish = True
        self.assertEqual(rig.at(330)[0], "on")
        count = len(rig.published)
        self.assertEqual(len(rig.log_lines("BedPressureRise publish failed")), 1)
        rig.fail_publish = False
        rig.at(335)
        self.assertEqual(len(rig.published), count + 1)
        self.assertEqual(rig.published[-1][1]["state"], "on")
        self.assertEqual(len(rig.log_lines("BedPressureRise on")), 1)


class Logging(unittest.TestCase):
    def test_one_line_per_output_transition_with_the_reason(self):
        rig = Rig()
        rig.sit_up()
        for seconds in (330, 335, 340):
            rig.at(seconds)
        self.assertEqual(rig.log_lines("BedPressureRise on"),
                         ["BedPressureRise on: left 54 >= baseline 13 + 40 for 20 s"])
        rig.put(LEFT_P, "30")
        for seconds in (341, 346):
            rig.at(seconds)
        self.assertEqual(rig.log_lines("BedPressureRise off"),
                         ["BedPressureRise off: left 30 < baseline 13 + 32"])

    def test_baseline_learned_is_logged_once_per_empty_run_not_on_every_relearn(self):
        rig = Rig()
        for seconds in (0, 100, 300, 305, 310, 600):
            rig.at(seconds)
        self.assertEqual(rig.log_lines("BedPressureRise baseline learned"),
                         ["BedPressureRise baseline learned: left 13, right 5"])
        rig.put(ROOM, "on")
        rig.at(601)
        rig.put(ROOM, "off")
        rig.at(602)
        rig.at(901)
        self.assertEqual(len(rig.log_lines("BedPressureRise baseline learned")), 1)
        rig.at(902)
        self.assertEqual(len(rig.log_lines("BedPressureRise baseline learned")), 2)


class Registration(unittest.TestCase):
    def test_listens_to_pressures_occupied_sensors_and_room_witnesses_only(self):
        rig = Rig()
        listened = {c.args[1] for c in rig.app.listen_state.call_args_list}
        self.assertEqual(listened, {LEFT_P, RIGHT_P, LEFT_OCC, RIGHT_OCC, ROOM, PIR})

    def test_ticks_every_five_seconds(self):
        rig = Rig()
        rig.app.run_every.assert_called_once()
        args = rig.app.run_every.call_args.args
        self.assertEqual(args[0], rig.app._tick)
        self.assertEqual(args[1], "now+5")
        self.assertEqual(args[2], 5)

    def test_events_and_ticks_evaluate_at_the_current_time(self):
        rig = Rig()
        rig.now = T0 + 300
        rig.app._tick({})
        self.assertEqual(rig.app._baseline["left"], 13.0)
        rig.put(LEFT_P, "21")
        rig.now += 1
        rig.app._on_change(LEFT_P, "state", "13", "21", {})
        self.assertEqual(rig.app._baseline["left"], 21.0)


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class ShippedConfig(unittest.TestCase):
    def read(self, relative):
        return (APPS / relative).read_text()

    def block(self, relative, name):
        return yaml.safe_load(self.read(relative))[name]

    def test_yaml_matches_the_class_and_names_no_log_stream(self):
        text = self.read("presence/bed_pressure_rise.yaml")
        self.assertNotRegex(text, r"(?m)^\s+log:")
        config = yaml.safe_load(text)["BedPressureRise"]
        self.assertEqual((config["module"], config["class"]), ("bed_pressure_rise", "BedPressureRise"))
        self.assertEqual({k: v for k, v in config.items() if k not in ("module", "class")}, BASE_ARGS)
        app = Rig(args=config).app
        self.assertEqual(app.output_entity, OUTPUT)

    def test_consumers_read_the_output_as_an_extra_witness(self):
        lists = (
            ("lights/bedroom_lights.yaml", "bedroom_lights", "withings_in_bed_entities"),
            ("rutines/mikkel_sleep_mode.yaml", "MikkelSleepMode", "in_bed_entities"),
            ("climate/smart_cooling.yaml", "SmartCooling", "bed_occupancy_sensors"),
            ("rutines/wakeup_reminder.yaml", "WakeupReminder", "in_bed_entities"),
        )
        for relative, name, key in lists:
            with self.subTest(file=relative):
                self.assertIn(OUTPUT, self.block(relative, name)[key])
        zones = self.block("presence/room_active.yaml", "RoomActive")["zones"]
        self.assertIn({"entity": OUTPUT, "tier": "bed"}, zones["bedroom"]["witnesses"])
        self.assertIn(OUTPUT, bedroom_evidence.WITNESSES)


if __name__ == "__main__":
    unittest.main()
