"""Unit tests for room_active's tiered-witness zone publisher (RoomActive)."""

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

import room_active  # noqa: E402

ZONE = "test_zone"
ZONE2 = "test_zone_2"
SETTLE_SEC = 15
HEARTBEAT_SEC = 60
REPUBLISH_AFTER_SEC = 120

ALL_ATTR_KEYS = {
    "reason", "tier", "confidence", "auto_on_ok", "active_witnesses",
    "source_entities", "witness_states", "settling", "stale", "computed_at",
}


def _witness(entity, tier, on_states=None):
    return {"entity": entity, "tier": tier, "on_states": list(on_states) if on_states else ["on"]}


WITNESSES = [
    _witness("binary_sensor.w_room", "room"),
    _witness("binary_sensor.w_spot", "spot"),
    _witness("binary_sensor.w_bed", "bed"),
    _witness("sensor.w_channel", "channel", ["large", "small"]),
]
W_ROOM, W_SPOT, W_BED, W_CHANNEL = (w["entity"] for w in WITNESSES)


def _fresh_zone_state(state):
    return {"state": state, "settle_timer": None, "last_cmp": None, "last_publish_time": None}


def make_app(zone_state="off", zones=None):
    """RoomActive with one configured zone (ZONE, one witness per tier) and fake AppDaemon
    callables, without running initialize(). All witnesses default to a readable, non-
    asserting "off" placeholder unless a test overrides app.states directly."""
    app = room_active.RoomActive.__new__(room_active.RoomActive)
    app.zones = zones if zones is not None else {ZONE: WITNESSES}
    app.settle_off_sec = SETTLE_SEC
    app.heartbeat_sec = HEARTBEAT_SEC
    app.republish_after_sec = REPUBLISH_AFTER_SEC
    app._zone_state = {z: _fresh_zone_state(zone_state) for z in app.zones}

    app.states = {}
    for witnesses in app.zones.values():
        for w in witnesses:
            app.states.setdefault((w["entity"], None), "off")

    def get_state(entity, attribute=None, **kw):
        return app.states.get((entity, attribute))

    app.get_state = get_state
    app.log = lambda *a, **kw: None
    app.run_in = MagicMock(return_value="settle-timer-handle")
    app.cancel_timer = MagicMock()
    app.set_state = MagicMock()
    return app


def make_multi_zone_app():
    return make_app(zones={ZONE: WITNESSES, ZONE2: [_witness("binary_sensor.w2_room", "room")]})


def _make_on_app():
    """A RoomActive already published "on" for ZONE via the room witness - ready for
    settle-timing tests. Mocks are reset after the seed publish."""
    app = make_app(zone_state=None)
    app.states[(W_ROOM, None)] = "on"
    app._evaluate(ZONE)
    app.set_state.reset_mock()
    app.run_in.reset_mock()
    app.cancel_timer.reset_mock()
    return app


BASE_ARGS = {
    "settle_off_sec": SETTLE_SEC,
    "heartbeat_sec": HEARTBEAT_SEC,
    "republish_after_sec": REPUBLISH_AFTER_SEC,
    "zones": {
        ZONE: {
            "witnesses": [
                {"entity": W_ROOM, "tier": "room"},
                {"entity": W_SPOT, "tier": "spot"},
                {"entity": W_BED, "tier": "bed"},
                {"entity": W_CHANNEL, "tier": "channel", "on_states": ["large", "small"]},
            ]
        },
    },
}


def make_full_app(overrides=None, states=None):
    """RoomActive with initialize() actually run (AppDaemon primitives stubbed only) - used
    to verify listener *registration* and initialize()-time startup publish."""
    app = room_active.RoomActive.__new__(room_active.RoomActive)
    app.args = dict(BASE_ARGS)
    if overrides:
        app.args.update(overrides)
    app.states = dict(states or {})

    def get_state(entity, attribute=None, **kw):
        return app.states.get((entity, attribute), "off")

    app.get_state = get_state
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock(return_value="heartbeat-handle")
    app.run_in = MagicMock(return_value="settle-timer-handle")
    app.cancel_timer = MagicMock()
    app.set_state = MagicMock()
    app.initialize()
    return app


class SingleWitnessAsserts(unittest.TestCase):
    """One witness of each tier asserting alone -> zone "on" with the correct reason/tier."""

    def _assert_on(self, entity, tier, on_state="on"):
        app = make_app(zone_state="off")
        app.states[(entity, None)] = on_state
        app._evaluate(ZONE)
        app.set_state.assert_called_once()
        args, kwargs = app.set_state.call_args
        self.assertEqual(args[0], f"binary_sensor.{ZONE}_active")
        self.assertEqual(kwargs["state"], "on")
        self.assertEqual(kwargs["attributes"]["reason"], f"{tier}:{entity}")
        self.assertEqual(kwargs["attributes"]["tier"], tier)
        return kwargs["attributes"]

    def test_room_witness_asserts(self):
        attrs = self._assert_on(W_ROOM, "room")
        self.assertEqual(attrs["confidence"], "high")

    def test_spot_witness_asserts(self):
        attrs = self._assert_on(W_SPOT, "spot")
        self.assertEqual(attrs["confidence"], "high")

    def test_bed_witness_asserts(self):
        attrs = self._assert_on(W_BED, "bed")
        self.assertEqual(attrs["confidence"], "high")

    def test_channel_witness_asserts(self):
        attrs = self._assert_on(W_CHANNEL, "channel", on_state="large")
        self.assertEqual(attrs["confidence"], "medium")


class ColdStart(unittest.TestCase):
    def test_nothing_asserting_at_cold_start_publishes_off_immediately_no_timer(self):
        app = make_app(zone_state=None)
        app._evaluate(ZONE)
        app.run_in.assert_not_called()
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "off")
        self.assertEqual(kwargs["attributes"]["settling"], "false")


class ImmediateOnTransition(unittest.TestCase):
    def test_off_to_on_is_immediate_no_timer(self):
        app = make_app(zone_state="off")
        app.states[(W_ROOM, None)] = "on"
        app._evaluate(ZONE)
        app.run_in.assert_not_called()
        app.set_state.assert_called_once()
        self.assertEqual(app.set_state.call_args.kwargs["state"], "on")


class SettleOffTiming(unittest.TestCase):
    def test_going_quiet_arms_settle_timer_state_stays_on_settling_true(self):
        app = _make_on_app()
        app.states[(W_ROOM, None)] = "off"
        app._evaluate(ZONE)
        app.run_in.assert_called_once_with(app._settle_fire, SETTLE_SEC, zone=ZONE)
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "on")
        self.assertEqual(kwargs["attributes"]["settling"], "true")
        self.assertEqual(kwargs["attributes"]["reason"], "all_clear")
        self.assertEqual(kwargs["attributes"]["tier"], "none")

    def test_repeated_quiet_events_do_not_rearm_a_second_timer(self):
        app = _make_on_app()
        app.states[(W_ROOM, None)] = "off"
        app._evaluate(ZONE)  # arms
        app._evaluate(ZONE)  # still quiet, must not arm again
        app.run_in.assert_called_once()

    def test_timer_fire_with_nothing_asserting_publishes_off(self):
        app = _make_on_app()
        app.states[(W_ROOM, None)] = "off"
        app._evaluate(ZONE)  # arms the timer
        app.set_state.reset_mock()

        app._settle_fire({"zone": ZONE})

        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "off")
        self.assertEqual(kwargs["attributes"]["settling"], "false")
        self.assertIsNone(app._zone_state[ZONE]["settle_timer"])

    def test_settling_attribute_false_once_fully_off(self):
        app = _make_on_app()
        app.states[(W_ROOM, None)] = "off"
        app._evaluate(ZONE)
        app._settle_fire({"zone": ZONE})
        self.assertEqual(app._zone_state[ZONE]["state"], "off")


class ReassertCancelsSettle(unittest.TestCase):
    def test_reassert_before_fire_cancels_timer_and_never_publishes_off(self):
        app = _make_on_app()
        app.states[(W_ROOM, None)] = "off"
        app._evaluate(ZONE)  # arms timer, settling=true
        timer_handle = app._zone_state[ZONE]["settle_timer"]
        self.assertIsNotNone(timer_handle)
        app.set_state.reset_mock()

        app.states[(W_ROOM, None)] = "on"  # reasserts before the timer fires
        app._evaluate(ZONE)

        app.cancel_timer.assert_called_once_with(timer_handle)
        self.assertIsNone(app._zone_state[ZONE]["settle_timer"])
        self.assertTrue(app.set_state.called)
        for c in app.set_state.call_args_list:
            self.assertEqual(c.kwargs["state"], "on")

    def test_different_witness_reasserting_updates_reason_mid_settle(self):
        app = _make_on_app()  # published via W_ROOM
        app.states[(W_ROOM, None)] = "off"
        app._evaluate(ZONE)  # settling starts
        app.set_state.reset_mock()

        app.states[(W_BED, None)] = "on"  # a different (higher-tier) witness reasserts
        app._evaluate(ZONE)

        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "on")
        self.assertEqual(kwargs["attributes"]["tier"], "bed")
        self.assertEqual(kwargs["attributes"]["settling"], "false")


class UnreadableWitnesses(unittest.TestCase):
    def test_unavailable_witness_never_asserts(self):
        app = make_app(zone_state="off")
        app.states[(W_ROOM, None)] = "unavailable"
        app._evaluate(ZONE)
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "off")
        self.assertEqual(kwargs["attributes"]["stale"], "true")

    def test_unknown_witness_never_asserts(self):
        app = make_app(zone_state="off")
        app.states[(W_SPOT, None)] = "unknown"
        app._evaluate(ZONE)
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "off")
        self.assertEqual(kwargs["attributes"]["stale"], "true")

    def test_missing_witness_state_never_asserts(self):
        app = make_app(zone_state="off")
        del app.states[(W_CHANNEL, None)]
        app._evaluate(ZONE)
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "off")
        self.assertEqual(kwargs["attributes"]["stale"], "true")

    def test_all_unreadable_while_on_holds_last_value_settles_normally(self):
        """Zone was "on"; every witness then goes unavailable at once - unreadable is inert,
        not denial, so this must follow the exact same settle path as any other all-quiet
        transition, not flip immediately."""
        app = _make_on_app()
        for w in WITNESSES:
            app.states[(w["entity"], None)] = "unavailable"
        app._evaluate(ZONE)
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "on")
        self.assertEqual(kwargs["attributes"]["stale"], "true")
        self.assertEqual(kwargs["attributes"]["settling"], "true")
        app.run_in.assert_called_once()

    def test_all_unreadable_while_already_off_holds_off(self):
        app = make_app(zone_state="off")
        for w in WITNESSES:
            app.states[(w["entity"], None)] = "unavailable"
        app._evaluate(ZONE)
        kwargs = app.set_state.call_args.kwargs
        self.assertEqual(kwargs["state"], "off")
        self.assertEqual(kwargs["attributes"]["stale"], "true")


class PayloadNeverFalsy(unittest.TestCase):
    """Regression guard for the AppDaemon 4.5.13 set_state falsy-attribute-drop bug - every
    published attribute must be a non-empty string, or a non-empty list of non-empty strings."""

    def _assert_all_truthy(self, attributes):
        self.assertEqual(set(attributes.keys()), ALL_ATTR_KEYS)
        for key, value in attributes.items():
            self.assertTrue(value, f"attribute {key!r} is falsy: {value!r}")
            if isinstance(value, list):
                for item in value:
                    self.assertIsInstance(item, str)
                    self.assertTrue(item)
            else:
                self.assertIsInstance(value, str)

    def test_all_off_settled(self):
        app = make_app(zone_state="off")
        app._evaluate(ZONE)
        self._assert_all_truthy(app.set_state.call_args.kwargs["attributes"])
        self.assertEqual(app.set_state.call_args.kwargs["attributes"]["active_witnesses"], ["<none>"])

    def test_all_on(self):
        app = make_app(zone_state="off")
        app.states[(W_ROOM, None)] = "on"
        app.states[(W_SPOT, None)] = "on"
        app.states[(W_BED, None)] = "on"
        app.states[(W_CHANNEL, None)] = "large"
        app._evaluate(ZONE)
        self._assert_all_truthy(app.set_state.call_args.kwargs["attributes"])

    def test_mixed(self):
        app = make_app(zone_state="off")
        app.states[(W_ROOM, None)] = "unavailable"
        app.states[(W_CHANNEL, None)] = "large"
        app._evaluate(ZONE)
        self._assert_all_truthy(app.set_state.call_args.kwargs["attributes"])

    def test_all_unavailable(self):
        app = make_app(zone_state="off")
        for w in WITNESSES:
            app.states[(w["entity"], None)] = "unavailable"
        app._evaluate(ZONE)
        self._assert_all_truthy(app.set_state.call_args.kwargs["attributes"])

    def test_mid_settle(self):
        app = _make_on_app()
        app.states[(W_ROOM, None)] = "off"
        app._evaluate(ZONE)
        self.assertEqual(app.set_state.call_args.kwargs["attributes"]["settling"], "true")
        self._assert_all_truthy(app.set_state.call_args.kwargs["attributes"])


class TierPriority(unittest.TestCase):
    def test_bed_wins_over_spot_room_channel(self):
        app = make_app(zone_state="off")
        app.states[(W_ROOM, None)] = "on"
        app.states[(W_SPOT, None)] = "on"
        app.states[(W_BED, None)] = "on"
        app.states[(W_CHANNEL, None)] = "large"
        app._evaluate(ZONE)
        attrs = app.set_state.call_args.kwargs["attributes"]
        self.assertEqual(attrs["tier"], "bed")
        self.assertEqual(attrs["reason"], f"bed:{W_BED}")
        self.assertEqual(set(attrs["active_witnesses"]), {W_ROOM, W_SPOT, W_BED, W_CHANNEL})

    def test_spot_wins_over_room_and_channel(self):
        app = make_app(zone_state="off")
        app.states[(W_ROOM, None)] = "on"
        app.states[(W_SPOT, None)] = "on"
        app.states[(W_CHANNEL, None)] = "large"
        app._evaluate(ZONE)
        attrs = app.set_state.call_args.kwargs["attributes"]
        self.assertEqual(attrs["tier"], "spot")
        self.assertEqual(attrs["reason"], f"spot:{W_SPOT}")

    def test_room_wins_over_channel(self):
        app = make_app(zone_state="off")
        app.states[(W_ROOM, None)] = "on"
        app.states[(W_CHANNEL, None)] = "large"
        app._evaluate(ZONE)
        attrs = app.set_state.call_args.kwargs["attributes"]
        self.assertEqual(attrs["tier"], "room")
        self.assertEqual(attrs["confidence"], "high")

    def test_channel_only_gives_medium_confidence(self):
        app = make_app(zone_state="off")
        app.states[(W_CHANNEL, None)] = "small"
        app._evaluate(ZONE)
        attrs = app.set_state.call_args.kwargs["attributes"]
        self.assertEqual(attrs["tier"], "channel")
        self.assertEqual(attrs["confidence"], "medium")


class Heartbeat(unittest.TestCase):
    def setUp(self):
        self._real_time = room_active.time
        self._fake_time = [1_800_000_000.0]
        room_active.time = types.SimpleNamespace(time=lambda: self._fake_time[0])
        self.addCleanup(self._restore_time)

    def _restore_time(self):
        room_active.time = self._real_time

    def test_unchanged_payload_does_not_republish_before_republish_after_sec(self):
        app = make_app(zone_state="off")
        app._evaluate(ZONE)  # baseline publish, seeds last_publish_time
        app.set_state.reset_mock()

        self._fake_time[0] += REPUBLISH_AFTER_SEC - 1
        app._heartbeat({})
        app.set_state.assert_not_called()

    def test_unchanged_payload_republishes_after_republish_after_sec_elapsed(self):
        import time as real_time

        app = make_app(zone_state="off")
        app._evaluate(ZONE)
        first_computed_at = app.set_state.call_args.kwargs["attributes"]["computed_at"]
        app.set_state.reset_mock()
        real_time.sleep(0.001)  # guarantee a distinguishable computed_at, belt and braces

        self._fake_time[0] += REPUBLISH_AFTER_SEC
        app._heartbeat({})
        app.set_state.assert_called_once()
        second_computed_at = app.set_state.call_args.kwargs["attributes"]["computed_at"]
        self.assertNotEqual(first_computed_at, second_computed_at)
        self.assertEqual(app.set_state.call_args.kwargs["state"], "off")

    def test_changed_payload_republishes_regardless_of_elapsed_time(self):
        app = make_app(zone_state="off")
        app._evaluate(ZONE)
        app.set_state.reset_mock()

        app.states[(W_ROOM, None)] = "on"  # a real change, well before republish_after_sec
        self._fake_time[0] += 1
        app._heartbeat({})
        app.set_state.assert_called_once()
        self.assertEqual(app.set_state.call_args.kwargs["state"], "on")

    def test_heartbeat_covers_every_zone(self):
        app = make_multi_zone_app()
        app._evaluate(ZONE)
        app._evaluate(ZONE2)
        app.set_state.reset_mock()

        self._fake_time[0] += REPUBLISH_AFTER_SEC
        app._heartbeat({})
        published = {c.args[0] for c in app.set_state.call_args_list}
        self.assertEqual(published, {f"binary_sensor.{ZONE}_active", f"binary_sensor.{ZONE2}_active"})


class PluginStarted(unittest.TestCase):
    def test_plugin_started_republishes_every_zone(self):
        app = make_multi_zone_app()
        app._on_plugin_started("plugin_started", {}, {})
        published = {c.args[0] for c in app.set_state.call_args_list}
        self.assertEqual(published, {f"binary_sensor.{ZONE}_active", f"binary_sensor.{ZONE2}_active"})

    def test_plugin_started_republishes_even_when_unchanged(self):
        app = make_multi_zone_app()
        app._on_plugin_started("plugin_started", {}, {})
        app.set_state.reset_mock()
        app._on_plugin_started("plugin_started", {}, {})  # nothing changed in between
        self.assertEqual(app.set_state.call_count, 2)


class InitializeRegistration(unittest.TestCase):
    def setUp(self):
        self.app = make_full_app()

    def test_listens_to_every_witness_with_zone_bound(self):
        calls = [
            c for c in self.app.listen_state.call_args_list
            if c.args and c.args[0] == self.app._on_witness_change
        ]
        entities = {c.args[1] for c in calls}
        self.assertEqual(entities, {W_ROOM, W_SPOT, W_BED, W_CHANNEL})
        for c in calls:
            self.assertEqual(c.kwargs.get("zone"), ZONE)

    def test_listens_for_plugin_started(self):
        calls = [
            c for c in self.app.listen_event.call_args_list
            if c.args and c.args[0] == self.app._on_plugin_started
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1], "plugin_started")

    def test_registers_heartbeat_run_every(self):
        self.app.run_every.assert_called_once()
        args = self.app.run_every.call_args.args
        self.assertEqual(args[0], self.app._heartbeat)
        self.assertEqual(args[2], HEARTBEAT_SEC)

    def test_initialize_publishes_immediately(self):
        self.app.set_state.assert_called_once()
        self.assertEqual(self.app.set_state.call_args.args[0], f"binary_sensor.{ZONE}_active")


if __name__ == "__main__":
    unittest.main()
