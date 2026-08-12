# tests/test_mikkel_sleep_mode.py - Reboot-survival fix for mikkel_sleep_mode.py
# (2026-07-27): _block_rearm_until_out_of_bed is persisted to mikkel_sleep_mode_state.json,
# and - because a persisted latch outlives the single live off edge that used to be its
# only release - reconciled against the bed at init and on every evaluation, but only ever
# against an EXPLICIT off on both cloud-backed bedsides. Same __new__ + monkeypatched-
# callables harness as the other apps/rutines/tests files, plus a couple of tests that call
# the real initialize() to prove a value written by one app instance is read back by a
# brand new one (the actual shape of an AD restart).
# Run from repo root: python3 -m unittest discover -s apps/rutines/tests -q

from __future__ import annotations

import json
import os
import sys
import tempfile
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


def make_app(states, *, block_rearm=False):
    """MikkelSleepMode with just the state _apply_sleep_mode/_on_relevant_change/
    _set_block_rearm_until_out_of_bed need, without running AppDaemon's initialize()."""
    app = msm.MikkelSleepMode.__new__(msm.MikkelSleepMode)
    app.battery_entity = "sensor.mikkels_ofx9p_battery_state"
    app.person_entity = "person.mikkel"
    app.in_bed_entities = ["binary_sensor.left_bedside", "binary_sensor.right_bedside"]
    app.sleep_mode_entity = "input_boolean.mikkel_sleep_mode"
    app._on_battery_states = frozenset(["charging", "not_charging"])
    app._off_battery_state = "discharging"
    # Turning sleep mode on also pushes command_dnd, so an app that gets that far needs
    # these too (they go through the same call_service mock).
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


ON_STATES = {
    "sensor.mikkels_ofx9p_battery_state": "charging",
    "person.mikkel": "home",
    "binary_sensor.left_bedside": "on",
    "binary_sensor.right_bedside": "off",
    "input_boolean.mikkel_sleep_mode": "off",
}


class SetBlockRearmPersists(unittest.TestCase):
    """The setter added for the restart-survival fix must update the in-memory flag,
    the state dict, and save - the same three things any caller needs from it."""

    def test_true_updates_flag_state_dict_and_saves(self):
        app = make_app(ON_STATES, block_rearm=False)
        app._set_block_rearm_until_out_of_bed(True)
        self.assertTrue(app._block_rearm_until_out_of_bed)
        self.assertEqual(app._state["block_rearm_until_out_of_bed"], True)
        app._save_state.assert_called_once()

    def test_false_updates_flag_state_dict_and_saves(self):
        app = make_app(ON_STATES, block_rearm=True)
        app._set_block_rearm_until_out_of_bed(False)
        self.assertFalse(app._block_rearm_until_out_of_bed)
        self.assertEqual(app._state["block_rearm_until_out_of_bed"], False)
        app._save_state.assert_called_once()


class OnSleepBooleanChangeSetsBlocker(unittest.TestCase):
    """Exercises the same condition initialize()'s nested _on_sleep_boolean_change uses
    (old == 'on' and sensors still allow sleep) against the persisting setter directly."""

    def test_blocks_when_sensors_still_allow_sleep(self):
        app = make_app(ON_STATES, block_rearm=False)
        if app._compute_want_on_raw():
            app._set_block_rearm_until_out_of_bed(True)
        self.assertTrue(app._block_rearm_until_out_of_bed)
        self.assertTrue(app._state["block_rearm_until_out_of_bed"])
        app._save_state.assert_called_once()

    def test_does_not_block_when_sensors_already_say_out_of_bed(self):
        states = dict(ON_STATES)
        states["binary_sensor.left_bedside"] = "off"
        states["binary_sensor.right_bedside"] = "off"
        app = make_app(states, block_rearm=False)
        if app._compute_want_on_raw():
            app._set_block_rearm_until_out_of_bed(True)
        self.assertFalse(app._block_rearm_until_out_of_bed)
        app._save_state.assert_not_called()


class BlockerReleasedWheneverTheBedReadsEmpty(unittest.TestCase):
    """The latch is released by any evaluation that reads the bed EXPLICITLY empty, not by
    an off edge on a specific bedside: the Withings sensors are cloud-backed and pass
    off->unknown->on, so the edge is routinely lost. Before this, one lost edge blocked
    sleep mode and phone DND until the next AD restart - and after b7c2d6a persisted the
    latch, forever. "Explicitly" is the other half: those same cloud sensors read
    unknown/unavailable/None whenever HA is restarting, which is not evidence he got up."""

    def test_clears_when_the_bed_reads_empty(self):
        states = dict(ON_STATES)
        states["binary_sensor.left_bedside"] = "off"  # both bedsides now off
        app = make_app(states, block_rearm=True)

        app._on_relevant_change("binary_sensor.left_bedside", "state", "on", "off", {})

        self.assertFalse(app._block_rearm_until_out_of_bed)
        self.assertFalse(app._state["block_rearm_until_out_of_bed"])
        app._save_state.assert_called_once()

    def test_clears_on_an_unrelated_callback_after_a_lost_off_edge(self):
        # The off edge never arrived (off->unknown->on on the cloud sensor); the next
        # battery/person callback must still let go.
        states = dict(ON_STATES)
        states["binary_sensor.left_bedside"] = "off"
        app = make_app(states, block_rearm=True)

        app._on_relevant_change(
            "sensor.mikkels_ofx9p_battery_state", "state", "charging", "discharging", {}
        )

        self.assertFalse(app._block_rearm_until_out_of_bed)
        self.assertTrue(any("releasing" in str(a) for a, kw in app.log_calls))

    def test_stays_blocked_while_any_in_bed_sensor_still_on(self):
        states = dict(ON_STATES)  # left_bedside "on", right_bedside "off"
        app = make_app(states, block_rearm=True)

        app._on_relevant_change("binary_sensor.right_bedside", "state", "on", "off", {})

        self.assertTrue(app._block_rearm_until_out_of_bed)
        app._save_state.assert_not_called()

    def test_release_cannot_re_arm_sleep_mode_by_itself(self):
        # The release condition is an empty bed, which is also a hard NO for sleep mode -
        # so letting go can never turn sleep back on behind the wakeup routine's back.
        states = dict(ON_STATES)
        states["binary_sensor.left_bedside"] = "off"
        app = make_app(states, block_rearm=True)

        app._apply_sleep_mode()

        turn_ons = [c for c in app.call_service.call_args_list if c[0][0] == "input_boolean/turn_on"]
        self.assertEqual(turn_ons, [])

    def test_an_unreadable_bed_never_releases_the_hold(self):
        # "unknown"/"unavailable"/missing entity is what the cloud bedsides read while HA
        # restarts or Withings reloads - not "he got up".
        for left, right in (
            ("unknown", "unknown"),
            ("unavailable", "off"),
            (None, "off"),
            ("off", "unavailable"),
        ):
            with self.subTest(left=left, right=right):
                states = dict(ON_STATES)
                states["binary_sensor.left_bedside"] = left
                states["binary_sensor.right_bedside"] = right
                app = make_app(states, block_rearm=True)

                app._apply_sleep_mode()

                self.assertTrue(app._block_rearm_until_out_of_bed)
                self.assertTrue(app._state["block_rearm_until_out_of_bed"])
                app._save_state.assert_not_called()


class ClearedHoldStaysClearedAndHeldHoldStaysHeld(unittest.TestCase):
    """The latch is PERSISTENT state, so what a single evaluation does to it only shows up
    on the NEXT one. Both directions are exercised across two evaluations here: a cloud
    dropout during the wake-up must not hand sleep mode back the moment the sensor returns,
    and a real off must not keep blocking it forever."""

    def _sequence(self, first_bedsides, block_rearm):
        """Evaluate once with the bed in some state, then again with him back on the
        sensor, charging and home - the moment a lost hold would re-arm sleep mode."""
        states = dict(ON_STATES)
        states["binary_sensor.left_bedside"], states["binary_sensor.right_bedside"] = first_bedsides
        app = make_app(states, block_rearm=block_rearm)
        app._apply_sleep_mode()
        states["binary_sensor.left_bedside"] = "on"
        states["binary_sensor.right_bedside"] = "off"
        app._apply_sleep_mode()
        return app, [c for c in app.call_service.call_args_list if c[0][0] == "input_boolean/turn_on"]

    def test_a_cloud_dropout_mid_wakeup_does_not_hand_sleep_mode_back(self):
        # The regression: unavailable bedsides read as "bed empty", the hold is cleared and
        # PERSISTED, and the next evaluation (or any restart after it) re-arms sleep mode
        # while the wakeup routine is running - phone straight back into DND.
        app, turn_ons = self._sequence(("unavailable", "unknown"), block_rearm=True)
        self.assertTrue(app._block_rearm_until_out_of_bed)
        self.assertEqual(turn_ons, [])

    def test_a_real_off_releases_the_hold_and_the_next_night_arms_normally(self):
        # Positive control for the test above: with an explicit off the hold does go, and
        # the same second evaluation then turns sleep mode on. Without this, the assertion
        # above could pass for the wrong reason.
        app, turn_ons = self._sequence(("off", "off"), block_rearm=True)
        self.assertFalse(app._block_rearm_until_out_of_bed)
        self.assertEqual(len(turn_ons), 1)


IN_BED_STATES = {
    "binary_sensor.left_bedside": "on",
    "binary_sensor.right_bedside": "off",
}
EMPTY_BED_STATES = {
    "binary_sensor.left_bedside": "off",
    "binary_sensor.right_bedside": "off",
}
# What the cloud-backed bedsides read while HA is coming back up - i.e. exactly when AD
# recreates this app and initialize() runs again.
UNREADABLE_BED_STATES = {
    "binary_sensor.left_bedside": "unavailable",
    "binary_sensor.right_bedside": "unknown",
}


class RestartSurvival(unittest.TestCase):
    """End-to-end: a value set by one app instance (before a simulated restart) must be
    read back by a brand new instance's real initialize() - the actual shape of an AD
    restart - but only while the bed still says he is lying there. Uses the real
    _load_state/_save_state (not stubbed)."""

    @staticmethod
    def _args(path):
        return {
            "state_file": path,
            "battery_entity": "sensor.mikkels_ofx9p_battery_state",
            "person_entity": "person.mikkel",
            "in_bed_entities": ["binary_sensor.left_bedside", "binary_sensor.right_bedside"],
            "sleep_mode_entity": "input_boolean.mikkel_sleep_mode",
            "notify_service": "notify.mobile_app_test",
        }

    def _new_app(self, path, states=None):
        app = msm.MikkelSleepMode.__new__(msm.MikkelSleepMode)
        app.args = self._args(path)
        app.listen_state = MagicMock()
        app.run_every = MagicMock()  # initialize() arms a minutely tick since the night gate
        app.log = MagicMock()
        app.get_state = lambda entity, **kw: (states or {}).get(entity)
        return app

    def _temp_state_file(self, contents=None):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        if contents is not None:
            with open(path, "w") as f:
                json.dump(contents, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_blocker_survives_a_simulated_restart_while_still_in_bed(self):
        path = self._temp_state_file()

        before_restart = self._new_app(path, IN_BED_STATES)
        before_restart.initialize()
        self.assertFalse(before_restart._block_rearm_until_out_of_bed)  # fresh install
        before_restart._set_block_rearm_until_out_of_bed(True)

        # "Restart": a brand new instance, nothing carried over except the file on disk.
        after_restart = self._new_app(path, IN_BED_STATES)
        after_restart.initialize()

        self.assertTrue(after_restart._block_rearm_until_out_of_bed)

    def test_restored_blocker_is_released_at_init_when_the_bed_is_empty(self):
        # He got up while AD was down, so the off edge that used to be the only release
        # never reached this app. A persisted latch would otherwise never let go.
        path = self._temp_state_file({"block_rearm_until_out_of_bed": True})

        app = self._new_app(path, EMPTY_BED_STATES)
        app.initialize()

        self.assertFalse(app._block_rearm_until_out_of_bed)
        with open(path) as f:
            self.assertFalse(json.load(f)["block_rearm_until_out_of_bed"])

    def test_restored_blocker_is_kept_at_init_when_the_bedsides_are_unreadable(self):
        # An HA restart erases nothing here (this is a file), but it does make the Withings
        # entities unavailable for a while - and it is an HA restart that makes AD recreate
        # this app in the first place. The reconcile must not read that as "out of bed".
        path = self._temp_state_file({"block_rearm_until_out_of_bed": True})

        app = self._new_app(path, UNREADABLE_BED_STATES)
        app.initialize()

        self.assertTrue(app._block_rearm_until_out_of_bed)
        with open(path) as f:
            self.assertTrue(json.load(f)["block_rearm_until_out_of_bed"])

    def test_cleared_blocker_also_survives_a_simulated_restart(self):
        path = self._temp_state_file({"block_rearm_until_out_of_bed": True})

        before_restart = self._new_app(path, IN_BED_STATES)
        before_restart.initialize()
        self.assertTrue(before_restart._block_rearm_until_out_of_bed)  # loaded True
        before_restart._set_block_rearm_until_out_of_bed(False)

        after_restart = self._new_app(path, IN_BED_STATES)
        after_restart.initialize()

        self.assertFalse(after_restart._block_rearm_until_out_of_bed)

    def test_fresh_install_with_no_state_file_defaults_to_false(self):
        app = self._new_app("/nonexistent/dir/mikkel_sleep_mode_state.json", EMPTY_BED_STATES)
        app.initialize()
        self.assertFalse(app._block_rearm_until_out_of_bed)


if __name__ == "__main__":
    unittest.main()
