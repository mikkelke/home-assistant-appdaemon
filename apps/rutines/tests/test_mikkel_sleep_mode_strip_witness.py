# tests/test_mikkel_sleep_mode_strip_witness.py - the ESPHome pressure strip
# (binary_sensor.bed_presence_6b9c94_bed_occupied_left, added 2026-08-12 with zero
# nights of track record) as an ADDITIONAL WITNESS in in_bed_entities:
#   - OR semantics: any exact "on" arms; unavailable/unknown/off strip is inert and
#     can never hold sleep mode on nor stop a Withings mat from arming it.
#   - bed_empty_entities (the mats only) is what releases the post-wakeup re-arm
#     hold: a stuck-on or offline strip must not deadlock the hold, because a stuck
#     hold blocks ALL future arming - the failure direction that matters.
# Same __new__ + monkeypatched-callables harness as the other tests here.
# Run from repo root: python3 -m unittest discover -s apps/rutines/tests -q

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

import mikkel_sleep_mode as msm  # noqa: E402

LEFT_MAT = "binary_sensor.left_bedside"
RIGHT_MAT = "binary_sensor.right_bedside"
STRIP = "binary_sensor.bed_presence_6b9c94_bed_occupied_left"
SLEEP = "input_boolean.mikkel_sleep_mode"


def make_app(states, *, block_rearm=False, mats_only_empty=True):
    """MikkelSleepMode with the production three-witness list (two mats + strip)
    and, matching the shipped yaml, bed_empty_entities = the mats only."""
    app = msm.MikkelSleepMode.__new__(msm.MikkelSleepMode)
    app.battery_entity = "sensor.mikkels_ofx9p_battery_state"
    app.person_entity = "person.mikkel"
    app.in_bed_entities = [LEFT_MAT, RIGHT_MAT, STRIP]
    app.bed_empty_entities = [LEFT_MAT, RIGHT_MAT] if mats_only_empty else None
    app.sleep_mode_entity = SLEEP
    app._on_battery_states = frozenset(["charging", "not_charging"])
    app._off_battery_state = "discharging"
    app._dnd_on_command = "priority_only"
    app._dnd_off_command = "off"
    app._notify_service_path = "notify/mobile_app_test"

    app.get_state = lambda entity, **kw: states.get(entity)
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.call_service = MagicMock()

    app._block_rearm_until_out_of_bed = block_rearm
    app._state = {"block_rearm_until_out_of_bed": block_rearm}
    app._save_state = MagicMock()
    return app


def base_states(overrides=None):
    states = {
        "sensor.mikkels_ofx9p_battery_state": "charging",
        "person.mikkel": "home",
        LEFT_MAT: "off",
        RIGHT_MAT: "off",
        STRIP: "off",
        SLEEP: "off",
    }
    states.update(overrides or {})
    return states


def sleep_writes(app, kind):
    return [
        c for c in app.call_service.call_args_list
        if c[0][0] == f"input_boolean/turn_{kind}" and c[1].get("entity_id") == SLEEP
    ]


class OrSemanticsWithTheStrip(unittest.TestCase):
    """_any_in_bed / the arming path across the strip's four interesting states."""

    def test_strip_on_alone_arms_sleep_mode(self):
        # The additional witness genuinely adds: strip catches him before the
        # cloud mats report anything.
        app = make_app(base_states({STRIP: "on"}))
        app._apply_sleep_mode()
        self.assertEqual(len(sleep_writes(app, "on")), 1)

    def test_strip_off_and_mats_off_does_not_arm(self):
        app = make_app(base_states())
        app._apply_sleep_mode()
        self.assertEqual(sleep_writes(app, "on"), [])

    def test_strip_unavailable_does_not_arm(self):
        app = make_app(base_states({STRIP: "unavailable"}))
        app._apply_sleep_mode()
        self.assertEqual(sleep_writes(app, "on"), [])

    def test_strip_unknown_does_not_arm(self):
        app = make_app(base_states({STRIP: "unknown"}))
        app._apply_sleep_mode()
        self.assertEqual(sleep_writes(app, "on"), [])

    def test_offline_strip_does_not_block_the_mat_from_arming(self):
        # The critical direction: the ESP flatlining unavailable at 03:00 must
        # leave the Withings mat's vote fully intact.
        app = make_app(base_states({LEFT_MAT: "on", STRIP: "unavailable"}))
        app._apply_sleep_mode()
        self.assertEqual(len(sleep_writes(app, "on")), 1)

    def test_offline_strip_does_not_hold_sleep_mode_on_by_day(self):
        # Sleep on, mats read off, strip unavailable, daytime (no house night
        # debounce) -> immediate clear. Non-"on" is off, never "maybe in bed".
        app = make_app(base_states({SLEEP: "on", STRIP: "unavailable"}))
        app._apply_sleep_mode()
        self.assertEqual(len(sleep_writes(app, "off")), 1)


class BedEmptyIgnoresTheStrip(unittest.TestCase):
    """_bed_reads_empty consults bed_empty_entities (the mats), not the strip."""

    def test_mats_off_strip_stuck_on_still_reads_empty(self):
        # A stuck-on strip must not deadlock the re-arm hold: mats explicitly
        # off = he got up, whatever the day-old strip claims.
        app = make_app(base_states({STRIP: "on"}))
        self.assertTrue(app._bed_reads_empty())

    def test_mats_off_strip_unavailable_still_reads_empty(self):
        app = make_app(base_states({STRIP: "unavailable"}))
        self.assertTrue(app._bed_reads_empty())

    def test_a_mat_still_on_keeps_the_bed_not_empty(self):
        app = make_app(base_states({LEFT_MAT: "on", STRIP: "off"}))
        self.assertFalse(app._bed_reads_empty())

    def test_mats_unavailable_is_not_empty(self):
        # Unchanged Withings semantics: cloud dropout is "unreadable", never
        # "he got up" - the hold survives HA restarts exactly as before.
        app = make_app(base_states({LEFT_MAT: "unavailable", RIGHT_MAT: "unavailable"}))
        self.assertFalse(app._bed_reads_empty())

    def test_without_bed_empty_entities_the_full_list_is_used(self):
        # Legacy fallback (bare instances / yaml without the key): every listed
        # sensor must read off - which is exactly why the shipped yaml pins
        # bed_empty_entities to the mats.
        app = make_app(base_states({STRIP: "on"}), mats_only_empty=False)
        self.assertFalse(app._bed_reads_empty())
        app2 = make_app(base_states(), mats_only_empty=False)
        self.assertTrue(app2._bed_reads_empty())


class HoldReleaseAndRearmWithTheStrip(unittest.TestCase):
    def test_offline_strip_does_not_block_hold_release_then_mat_rearms(self):
        # Post-wakeup hold active, he got up (mats off), strip offline. The hold
        # must release on the mats alone; when he is back in bed tonight the mat
        # arms sleep mode again.
        states = base_states({STRIP: "unavailable"})
        app = make_app(states, block_rearm=True)
        app._apply_sleep_mode()
        self.assertFalse(app._block_rearm_until_out_of_bed)
        states[LEFT_MAT] = "on"
        app._apply_sleep_mode()
        self.assertEqual(len(sleep_writes(app, "on")), 1)

    def test_stuck_on_strip_does_not_block_hold_release(self):
        states = base_states({STRIP: "on"})
        app = make_app(states, block_rearm=True)
        app._release_rearm_hold_if_out_of_bed("test")
        self.assertFalse(app._block_rearm_until_out_of_bed)

    def test_hold_stays_while_a_mat_reads_on(self):
        states = base_states({LEFT_MAT: "on"})
        app = make_app(states, block_rearm=True)
        app._release_rearm_hold_if_out_of_bed("test")
        self.assertTrue(app._block_rearm_until_out_of_bed)


if __name__ == "__main__":
    unittest.main()
