"""Unit tests for presence_trust - distrust rules recomputed from RAW sensors.

Evidence encoded (measured 2026-08-05..08-11 on this install):
- 2026-08-09 16:01->18:40Z: kitchen mmWave-only presence for 159 min with the
  PIR never firing once while the kitchen speaker played - the ghost signature
  (575 min of it across the week, 88 min with island+counter lights on).
- A real entrance fires the PIR at walk-in, so a PIR off-edge *inside* the
  mmWave-on span must clear suspicion for the whole continuous span.

The helper reads the raw composite + members, never binary_sensor.presence_*
(set_state entities vanish on HA restart; their `suspect` attribute also drops
from attributes when False - AD 4.5.13 set_state bug).
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

import presence_trust  # noqa: E402

COMPOSITE = "binary_sensor.kitchen_pir_presence"
MMWAVE = "binary_sensor.kitchen_presence_presence"
PIR = "binary_sensor.kitchen_presence_pir_detection"
SPEAKER = "media_player.kitchen_2"

BED_COMPOSITE = "binary_sensor.bedroom_pir_presence"
BED_MMWAVE = "binary_sensor.bedroom_presence_presence"
BED_FP300_PIR = "binary_sensor.bedroom_presence_pir_detection"
BED_LEFT = "binary_sensor.left_bedside"
BED_RIGHT = "binary_sensor.right_bedside"
AC_POWER = "sensor.air_conditioner_real_time_power"

BATH_COMPOSITE = "binary_sensor.bathroom_pir_presence"


def utc(hour, minute=0, second=0, day=9):
    return datetime(2026, 8, day, hour, minute, second, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def ep(dt):
    return dt.timestamp()


class FakeHass:
    def __init__(self, states):
        self.states = dict(states)

    def get_state(self, entity, attribute=None, **kw):
        ent = self.states.get(entity)
        if ent is None:
            return None
        if attribute is None:
            return ent.get("state")
        if attribute == "last_changed":
            return ent.get("last_changed")
        if attribute == "all":
            return {
                "attributes": ent.get("attributes", {}),
                "last_changed": ent.get("last_changed"),
            }
        return ent.get("attributes", {}).get(attribute)


def kitchen_states(
    mmwave_on_since=utc(16, 1),
    pir_state="off",
    pir_last_changed=utc(15, 35),
    speaker="playing",
    composite="on",
):
    return {
        COMPOSITE: {
            "state": composite,
            "attributes": {"entity_id": [MMWAVE, PIR]},
            "last_changed": iso(mmwave_on_since),
        },
        MMWAVE: {"state": "on", "last_changed": iso(mmwave_on_since)},
        PIR: {"state": pir_state, "last_changed": iso(pir_last_changed)},
        SPEAKER: {"state": speaker},
    }


def suspect(states, now, room="kitchen", composite=COMPOSITE, **kw):
    return presence_trust.presence_suspect(
        FakeHass(states), room, composite, now=now, **kw
    )


class KitchenGhostEpisode(unittest.TestCase):
    """The measured 2026-08-09 16:01->18:40Z mmWave-only episode."""

    def test_ghost_episode_is_suspect(self):
        res = suspect(kitchen_states(), now=ep(utc(18, 40)))
        self.assertTrue(res.suspect)
        self.assertIn("speaker", res.reason)

    def test_suspect_shortly_after_duration_guard(self):
        res = suspect(kitchen_states(), now=ep(utc(16, 12)))
        self.assertTrue(res.suspect)

    def test_not_yet_suspect_before_duration_guard(self):
        res = suspect(kitchen_states(), now=ep(utc(16, 5)))
        self.assertFalse(res.suspect)

    def test_default_duration_guard_is_ten_minutes(self):
        self.assertEqual(presence_trust.DEFAULT_SUSPECT_AFTER_MINUTES, 10.0)

    def test_custom_duration_guard(self):
        res = suspect(
            kitchen_states(), now=ep(utc(16, 4)), suspect_after_minutes=2
        )
        self.assertTrue(res.suspect)


class RealPresenceClearsSuspicion(unittest.TestCase):
    def test_pir_off_edge_inside_span_clears_whole_span(self):
        """Real entrance: PIR fired (off-edge 16:30 > mmWave on-edge 16:01)."""
        states = kitchen_states(pir_last_changed=utc(16, 30))
        res = suspect(states, now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)
        self.assertIn("fired within", res.reason)

    def test_pir_currently_on_clears(self):
        states = kitchen_states(pir_state="on", pir_last_changed=utc(18, 0))
        res = suspect(states, now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)

    def test_mmwave_off_composite_held_by_pir_is_real(self):
        states = kitchen_states(pir_state="on", pir_last_changed=utc(18, 0))
        states[MMWAVE] = {"state": "off", "last_changed": iso(utc(17, 0))}
        res = suspect(states, now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)
        self.assertIn("non-marker", res.reason)


class InterfererGate(unittest.TestCase):
    def test_speaker_idle_not_suspect(self):
        res = suspect(kitchen_states(speaker="idle"), now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)
        self.assertIn("interferer inactive", res.reason)

    def test_speaker_paused_not_suspect(self):
        res = suspect(kitchen_states(speaker="paused"), now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)


class CompositeAndRuleGates(unittest.TestCase):
    def test_composite_off_not_suspect(self):
        res = suspect(kitchen_states(composite="off"), now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)

    def test_room_without_rule_never_suspect(self):
        res = suspect(
            kitchen_states(),
            now=ep(utc(18, 40)),
            room="living_room",
            composite=COMPOSITE,
        )
        self.assertFalse(res.suspect)
        self.assertIn("no distrust rule", res.reason)


class TrustBiasOnUnreadableInputs(unittest.TestCase):
    def test_marker_on_edge_unknown_trusts(self):
        states = kitchen_states()
        states[MMWAVE] = {"state": "on", "last_changed": None}
        res = suspect(states, now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)

    def test_pir_entity_missing_trusts(self):
        states = kitchen_states()
        del states[PIR]
        res = suspect(states, now=ep(utc(18, 40)))
        self.assertFalse(res.suspect)
        self.assertIn("unknown - trusting", res.reason)

    def test_get_state_raising_trusts(self):
        class Boom:
            def get_state(self, *a, **kw):
                raise RuntimeError("hass down")

        res = presence_trust.presence_suspect(
            Boom(), "kitchen", COMPOSITE, now=ep(utc(18, 40))
        )
        self.assertFalse(res.suspect)

    def test_z_suffix_timestamp_parses(self):
        states = kitchen_states()
        states[MMWAVE]["last_changed"] = "2026-08-09T16:01:00Z"
        res = suspect(states, now=ep(utc(18, 40)))
        self.assertTrue(res.suspect)


class BedroomRuleHelperOnly(unittest.TestCase):
    """Implemented but NOT wired to consumers (AC un-deployed, unvalidated)."""

    def _bed_states(self, power="120", left="off", right="off"):
        return {
            BED_COMPOSITE: {
                "state": "on",
                "attributes": {
                    "entity_id": [BED_MMWAVE, BED_FP300_PIR, BED_LEFT, BED_RIGHT]
                },
                "last_changed": iso(utc(16, 0)),
            },
            BED_MMWAVE: {"state": "on", "last_changed": iso(utc(16, 0))},
            BED_FP300_PIR: {"state": "off", "last_changed": iso(utc(15, 0))},
            BED_LEFT: {"state": left, "last_changed": iso(utc(15, 0))},
            BED_RIGHT: {"state": right, "last_changed": iso(utc(15, 0))},
            AC_POWER: {"state": power},
        }

    def test_ac_running_mmwave_only_is_suspect(self):
        res = suspect(
            self._bed_states(), now=ep(utc(16, 20)), room="bedroom",
            composite=BED_COMPOSITE,
        )
        self.assertTrue(res.suspect)

    def test_bedside_sensor_on_is_real_presence(self):
        res = suspect(
            self._bed_states(left="on"), now=ep(utc(16, 20)), room="bedroom",
            composite=BED_COMPOSITE,
        )
        self.assertFalse(res.suspect)

    def test_ac_power_below_threshold_not_suspect(self):
        res = suspect(
            self._bed_states(power="30"), now=ep(utc(16, 20)), room="bedroom",
            composite=BED_COMPOSITE,
        )
        self.assertFalse(res.suspect)

    def test_ac_power_unavailable_not_suspect(self):
        res = suspect(
            self._bed_states(power="unavailable"), now=ep(utc(16, 20)),
            room="bedroom", composite=BED_COMPOSITE,
        )
        self.assertFalse(res.suspect)


class BathroomTemplateFallback(unittest.TestCase):
    """Bathroom composite is a template - members not introspectable. Mirrors
    presence_model: suspect-eligible from the composite's own on-edge."""

    def _bath_states(self, power="120", on_since=utc(16, 1)):
        return {
            BATH_COMPOSITE: {
                "state": "on",
                "attributes": {},
                "last_changed": iso(on_since),
            },
            AC_POWER: {"state": power},
        }

    def test_template_composite_suspect_after_guard(self):
        res = suspect(
            self._bath_states(), now=ep(utc(16, 20)), room="bathroom",
            composite=BATH_COMPOSITE,
        )
        self.assertTrue(res.suspect)
        self.assertIn("not introspectable", res.reason)

    def test_template_composite_not_yet_suspect(self):
        res = suspect(
            self._bath_states(), now=ep(utc(16, 5)), room="bathroom",
            composite=BATH_COMPOSITE,
        )
        self.assertFalse(res.suspect)


if __name__ == "__main__":
    unittest.main()
