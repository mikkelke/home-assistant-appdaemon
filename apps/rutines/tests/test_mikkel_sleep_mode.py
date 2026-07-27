# tests/test_mikkel_sleep_mode.py - Reboot-survival fix for mikkel_sleep_mode.py
# (2026-07-27): _block_rearm_until_out_of_bed is persisted to mikkel_sleep_mode_state.json
# at the same two call sites the in-memory flag was already set/cleared. Same __new__ +
# monkeypatched-callables harness as the other apps/rutines/tests files, plus a couple of
# tests that call the real initialize() to prove a value written by one app instance is
# read back by a brand new one (the actual shape of an AD restart).
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


class OnRelevantChangeClearsBlocker(unittest.TestCase):
    def test_clears_when_last_in_bed_sensor_goes_off(self):
        states = dict(ON_STATES)
        states["binary_sensor.left_bedside"] = "off"  # both bedsides now off
        app = make_app(states, block_rearm=True)
        app._apply_sleep_mode = MagicMock()

        app._on_relevant_change("binary_sensor.left_bedside", "state", "on", "off", {})

        self.assertFalse(app._block_rearm_until_out_of_bed)
        self.assertFalse(app._state["block_rearm_until_out_of_bed"])
        app._save_state.assert_called_once()
        app._apply_sleep_mode.assert_called_once()

    def test_stays_blocked_while_any_in_bed_sensor_still_on(self):
        states = dict(ON_STATES)  # left_bedside "on", right_bedside "off"
        app = make_app(states, block_rearm=True)
        app._apply_sleep_mode = MagicMock()

        app._on_relevant_change("binary_sensor.right_bedside", "state", "on", "off", {})

        self.assertTrue(app._block_rearm_until_out_of_bed)
        app._save_state.assert_not_called()

    def test_unrelated_entity_change_does_not_touch_blocker(self):
        app = make_app(ON_STATES, block_rearm=True)
        app._apply_sleep_mode = MagicMock()

        app._on_relevant_change(
            "sensor.mikkels_ofx9p_battery_state", "state", "not_charging", "charging", {}
        )

        self.assertTrue(app._block_rearm_until_out_of_bed)
        app._save_state.assert_not_called()


class RestartSurvival(unittest.TestCase):
    """End-to-end: a value set by one app instance (before a simulated restart) must be
    read back by a brand new instance's real initialize() - the actual behavior an AD
    restart needs. Uses the real _load_state/_save_state (not stubbed)."""

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

    def _new_app(self, path):
        app = msm.MikkelSleepMode.__new__(msm.MikkelSleepMode)
        app.args = self._args(path)
        app.listen_state = MagicMock()
        app.log = MagicMock()
        return app

    def test_blocker_survives_a_simulated_restart(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        before_restart = self._new_app(path)
        before_restart.initialize()
        self.assertFalse(before_restart._block_rearm_until_out_of_bed)  # fresh install
        before_restart._set_block_rearm_until_out_of_bed(True)

        # "Restart": a brand new instance, nothing carried over except the file on disk.
        after_restart = self._new_app(path)
        after_restart.initialize()

        self.assertTrue(after_restart._block_rearm_until_out_of_bed)

    def test_cleared_blocker_also_survives_a_simulated_restart(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump({"block_rearm_until_out_of_bed": True}, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        before_restart = self._new_app(path)
        before_restart.initialize()
        self.assertTrue(before_restart._block_rearm_until_out_of_bed)  # loaded True
        before_restart._set_block_rearm_until_out_of_bed(False)

        after_restart = self._new_app(path)
        after_restart.initialize()

        self.assertFalse(after_restart._block_rearm_until_out_of_bed)

    def test_fresh_install_with_no_state_file_defaults_to_false(self):
        app = self._new_app("/nonexistent/dir/mikkel_sleep_mode_state.json")
        app.initialize()
        self.assertFalse(app._block_rearm_until_out_of_bed)


if __name__ == "__main__":
    unittest.main()
