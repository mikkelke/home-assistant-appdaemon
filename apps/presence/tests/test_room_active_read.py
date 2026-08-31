"""Unit tests for room_active_read's plain-function reader (active() / explicitly_inactive())."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import room_active_read as rar  # noqa: E402

ZONE = "kitchen"
ENTITY = f"binary_sensor.{ZONE}_active"


class FakeHass:
    """Minimal duck-typed stand-in for an AppDaemon Hass instance - room_active_read only
    ever calls get_state/get_app, exactly what this fakes."""

    def __init__(self, states=None, apps=None):
        self.states = dict(states or {})
        self.apps = dict(apps or {})

    def get_state(self, entity, attribute=None, **kwargs):
        return self.states.get((entity, attribute))

    def get_app(self, name):
        return self.apps.get(name)


def _iso(seconds_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _room_active_app(zones):
    return SimpleNamespace(zones=zones)


def _witness(entity, tier="room", on_states=None):
    return {"entity": entity, "tier": tier, "on_states": list(on_states) if on_states else ["on"]}


class FreshPublishedEntity(unittest.TestCase):
    """A fresh, well-formed published entity is trusted directly - no fallback triggered
    (proved here by never registering a RoomActive app at all)."""

    def test_fresh_on_returns_true(self):
        hass = FakeHass(states={(ENTITY, None): "on", (ENTITY, "computed_at"): _iso(5)})
        self.assertTrue(rar.active(hass, ZONE))

    def test_fresh_off_returns_false(self):
        hass = FakeHass(states={(ENTITY, None): "off", (ENTITY, "computed_at"): _iso(5)})
        self.assertFalse(rar.active(hass, ZONE))

    def test_within_custom_max_age_sec_is_trusted(self):
        hass = FakeHass(states={(ENTITY, None): "on", (ENTITY, "computed_at"): _iso(50)})
        self.assertTrue(rar.active(hass, ZONE, max_age_sec=60))


class FallbackTriggers(unittest.TestCase):
    """Missing / stale / malformed computed_at all fall back to recomputing from
    RoomActive.zones rather than trusting the published entity."""

    def test_missing_published_entity_falls_back(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app({ZONE: [_witness("binary_sensor.w1")]})})
        hass.states[("binary_sensor.w1", None)] = "on"
        self.assertTrue(rar.active(hass, ZONE))

    def test_stale_computed_at_falls_back(self):
        hass = FakeHass(
            states={(ENTITY, None): "on", (ENTITY, "computed_at"): _iso(600)},
            apps={"RoomActive": _room_active_app({ZONE: [_witness("binary_sensor.w1")]})},
        )
        hass.states[("binary_sensor.w1", None)] = "off"
        # published entity says "on" but is older than the default max_age_sec (300) - must
        # NOT be trusted; the fallback witness correctly says False, proving it was ignored.
        self.assertFalse(rar.active(hass, ZONE))

    def test_malformed_computed_at_falls_back(self):
        hass = FakeHass(
            states={(ENTITY, None): "on", (ENTITY, "computed_at"): "not-a-timestamp"},
            apps={"RoomActive": _room_active_app({ZONE: [_witness("binary_sensor.w1")]})},
        )
        hass.states[("binary_sensor.w1", None)] = "off"
        self.assertFalse(rar.active(hass, ZONE))

    def test_missing_computed_at_falls_back(self):
        hass = FakeHass(
            states={(ENTITY, None): "on"},
            apps={"RoomActive": _room_active_app({ZONE: [_witness("binary_sensor.w1")]})},
        )
        hass.states[("binary_sensor.w1", None)] = "off"
        self.assertFalse(rar.active(hass, ZONE))

    def test_published_state_unavailable_falls_back(self):
        hass = FakeHass(
            states={(ENTITY, None): "unavailable"},
            apps={"RoomActive": _room_active_app({ZONE: [_witness("binary_sensor.w1")]})},
        )
        hass.states[("binary_sensor.w1", None)] = "on"
        self.assertTrue(rar.active(hass, ZONE))


class FallbackRecompute(unittest.TestCase):
    """The fallback path reproduces the publisher's own union-of-witnesses rule exactly."""

    def test_single_witness_asserting_true(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app(
            {ZONE: [_witness("binary_sensor.w1"), _witness("binary_sensor.w2")]}
        )})
        hass.states[("binary_sensor.w1", None)] = "off"
        hass.states[("binary_sensor.w2", None)] = "on"
        self.assertTrue(rar.active(hass, ZONE))

    def test_all_readable_none_asserting_false(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app(
            {ZONE: [_witness("binary_sensor.w1"), _witness("binary_sensor.w2")]}
        )})
        hass.states[("binary_sensor.w1", None)] = "off"
        hass.states[("binary_sensor.w2", None)] = "off"
        self.assertFalse(rar.active(hass, ZONE))

    def test_all_unreadable_returns_none(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app(
            {ZONE: [_witness("binary_sensor.w1"), _witness("binary_sensor.w2")]}
        )})
        hass.states[("binary_sensor.w1", None)] = "unavailable"
        hass.states[("binary_sensor.w2", None)] = "unknown"
        self.assertIsNone(rar.active(hass, ZONE))

    def test_mix_of_unreadable_and_readable_not_asserting_is_false(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app(
            {ZONE: [_witness("binary_sensor.w1"), _witness("binary_sensor.w2")]}
        )})
        hass.states[("binary_sensor.w1", None)] = "unavailable"
        hass.states[("binary_sensor.w2", None)] = "off"
        self.assertFalse(rar.active(hass, ZONE))

    def test_custom_on_states_respected(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app(
            {ZONE: [_witness("sensor.w1", on_states=["large", "small"])]}
        )})
        hass.states[("sensor.w1", None)] = "large"
        self.assertTrue(rar.active(hass, ZONE))

    def test_zone_missing_from_app_returns_none(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app({"other_zone": [_witness("binary_sensor.w1")]})})
        self.assertIsNone(rar.active(hass, ZONE))

    def test_zone_with_empty_witness_list_returns_none(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app({ZONE: []})})
        self.assertIsNone(rar.active(hass, ZONE))


class AppNotLoaded(unittest.TestCase):
    def test_get_app_returns_none(self):
        hass = FakeHass()  # RoomActive never registered
        self.assertIsNone(rar.active(hass, ZONE))

    def test_get_app_raises(self):
        class RaisingHass(FakeHass):
            def get_app(self, name):
                raise RuntimeError("boom")

        hass = RaisingHass()
        self.assertIsNone(rar.active(hass, ZONE))


class ExplicitlyInactive(unittest.TestCase):
    """True ONLY on a confirmed-fresh False - never True from missing/stale data or an
    exception, whatever the raw published value happened to say."""

    def test_true_only_on_confirmed_false(self):
        hass = FakeHass(states={(ENTITY, None): "off", (ENTITY, "computed_at"): _iso(5)})
        self.assertTrue(rar.explicitly_inactive(hass, ZONE))

    def test_false_on_confirmed_true(self):
        hass = FakeHass(states={(ENTITY, None): "on", (ENTITY, "computed_at"): _iso(5)})
        self.assertFalse(rar.explicitly_inactive(hass, ZONE))

    def test_false_on_none_from_fallback(self):
        hass = FakeHass(apps={"RoomActive": _room_active_app(
            {ZONE: [_witness("binary_sensor.w1")]}
        )})
        hass.states[("binary_sensor.w1", None)] = "unavailable"
        self.assertFalse(rar.explicitly_inactive(hass, ZONE))

    def test_false_on_missing_entity(self):
        hass = FakeHass()  # nothing published, no RoomActive app -> active() is None
        self.assertFalse(rar.explicitly_inactive(hass, ZONE))

    def test_false_on_stale_entity(self):
        # Stale published "off" AND no RoomActive app for fallback -> active() is None, so
        # even a raw "off" reading must not be trusted as explicit absence.
        hass = FakeHass(states={(ENTITY, None): "off", (ENTITY, "computed_at"): _iso(9999)})
        self.assertFalse(rar.explicitly_inactive(hass, ZONE))

    def test_false_on_injected_exception(self):
        class RaisingHass(FakeHass):
            def get_state(self, entity, attribute=None, **kwargs):
                raise RuntimeError("boom")

        hass = RaisingHass()
        self.assertFalse(rar.explicitly_inactive(hass, ZONE))


if __name__ == "__main__":
    unittest.main()
