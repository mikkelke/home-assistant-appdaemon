"""FamilyRoomLights manual-bright detection vs the app's own service-call echoes.

Defect (measured 2026-07-22..08-12): _on_manual_bright_light_change treated ANY
non-empty context.user_id as a human, but AppDaemon's own service calls carry
the install's service-account id. The Zigbee/Hue state-report lag on this
install is p50 2.82 s / p90 5.71 s / max 9.98 s, so the old 1.5 s echo window
missed 188 of 231 "Manual bright" events in 3 weeks - each one latched
_sleep_activated_during_presence and froze the decision tree on
preserve_current_state until presence was lost.

Fix under test: (a) context.user_id == appdaemon_user_id (yaml knob) is
automation, never manual; (b) echo window raised to 8 s. Genuine wall presses
carry user_id None (and their own event.* entities elsewhere) - unchanged.
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))

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

import family_room_lights as fr  # noqa: E402

LIGHT = "light.living_room_corner_light"
AD_ID = "f4fda494358943beaf8ad2c70db099f8"
HUMAN_ID = "0123456789abcdef0123456789abcdef"

# Measured state-report lag distribution (seconds) on this installation.
LAG_P50, LAG_P90, LAG_MAX = 2.82, 5.71, 9.98
ECHO_WINDOW_S = 8.0


class FakeClock:
    def __init__(self, start=100.0):
        self.now = start

    def monotonic(self):
        return self.now


def make_app(context_uid, echo_seconds=ECHO_WINDOW_S, ad_id=AD_ID):
    app = fr.FamilyRoomLights.__new__(fr.FamilyRoomLights)
    app._manual_bright_watch = {LIGHT}
    app._manual_bright_echo_until = {}
    app._manual_bright_echo_seconds = echo_seconds
    app._appdaemon_user_id = ad_id
    app._sleep_activated_during_presence = False

    app.log_calls = []
    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
    app.evaluations = []
    app._schedule_evaluation = lambda **kw: app.evaluations.append(kw)
    app._has_family_room_presence = lambda: True
    app.get_state = lambda entity, attribute=None, **kw: (
        {"context": {"user_id": context_uid}} if attribute == "all" else None
    )
    return app


def fire(app, entity=LIGHT):
    app._on_manual_bright_light_change(entity, None, "off", "on", {})


def assert_no_errors(test, app):
    errors = [(lvl, msg) for lvl, msg in app.log_calls if lvl == "ERROR"]
    test.assertEqual(errors, [])


class MeasuredLagDistributionNeverLatches(unittest.TestCase):
    """App command at t0; own echoes arrive at p50/p90/max lag with the AD id."""

    def setUp(self):
        self.clock = FakeClock(100.0)
        self._real_time = fr.time
        fr.time = types.SimpleNamespace(
            monotonic=self.clock.monotonic, time=self._real_time.time
        )

    def tearDown(self):
        fr.time = self._real_time

    def test_own_echoes_at_p50_p90_max_never_latch(self):
        app = make_app(context_uid=AD_ID)
        app._note_app_light_command(LIGHT)  # app turns the light on at t=100

        for lag in (LAG_P50, LAG_P90, LAG_MAX):
            self.clock.now = 100.0 + lag
            fire(app)
            self.assertFalse(
                app._sleep_activated_during_presence,
                f"own echo at +{lag}s latched manual bright",
            )
        self.assertEqual(app.evaluations, [])
        assert_no_errors(self, app)

    def test_p50_and_p90_are_inside_echo_window_max_is_not(self):
        """Documents the split of defences: 8 s window covers p50+p90; the
        max-lag echo (9.98 s) escapes the window and only the id check stops it."""
        app = make_app(context_uid=AD_ID)
        app._note_app_light_command(LIGHT)

        self.clock.now = 100.0 + LAG_P50
        self.assertTrue(app._manual_bright_echo_active(LIGHT))
        self.clock.now = 100.0 + LAG_P90
        self.assertTrue(app._manual_bright_echo_active(LIGHT))
        self.clock.now = 100.0 + LAG_MAX
        self.assertFalse(app._manual_bright_echo_active(LIGHT))

    def test_max_lag_echo_with_old_window_only_id_check_saves_it(self):
        """With the old 1.5 s window even the p50 echo latched; the id check
        must stop every own echo regardless of window size."""
        app = make_app(context_uid=AD_ID, echo_seconds=1.5)
        app._note_app_light_command(LIGHT)
        self.clock.now = 100.0 + LAG_P50  # outside the old 1.5 s window
        fire(app)
        self.assertFalse(app._sleep_activated_during_presence)
        self.assertEqual(app.evaluations, [])


class AppDaemonContextNeverManual(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(100.0)
        self._real_time = fr.time
        fr.time = types.SimpleNamespace(
            monotonic=self.clock.monotonic, time=self._real_time.time
        )

    def tearDown(self):
        fr.time = self._real_time

    def test_ad_id_without_any_echo_window_never_latches(self):
        """No _note_app_light_command at all (e.g. AD restarted in between):
        the service-account id alone must classify it as automation."""
        app = make_app(context_uid=AD_ID)
        fire(app)
        self.assertFalse(app._sleep_activated_during_presence)
        self.assertEqual(app.evaluations, [])
        assert_no_errors(self, app)

    def test_wall_press_none_context_still_ignored_here(self):
        """Physical presses surface with user_id None + their own event.*
        entities (handled elsewhere) - this handler must keep ignoring them."""
        app = make_app(context_uid=None)
        fire(app)
        self.assertFalse(app._sleep_activated_during_presence)
        self.assertEqual(app.evaluations, [])


class RealHumanPressesStillLatch(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(100.0)
        self._real_time = fr.time
        fr.time = types.SimpleNamespace(
            monotonic=self.clock.monotonic, time=self._real_time.time
        )

    def tearDown(self):
        fr.time = self._real_time

    def test_human_dashboard_press_latches_manual_bright(self):
        app = make_app(context_uid=HUMAN_ID)
        fire(app)
        self.assertTrue(app._sleep_activated_during_presence)
        self.assertEqual(len(app.evaluations), 1)
        self.assertTrue(
            any("Manual bright" in msg for _lvl, msg in app.log_calls)
        )

    def test_human_press_just_after_echo_window_latches(self):
        app = make_app(context_uid=HUMAN_ID)
        app._note_app_light_command(LIGHT)
        self.clock.now = 100.0 + ECHO_WINDOW_S + 0.5
        fire(app)
        self.assertTrue(app._sleep_activated_during_presence)

    def test_residual_risk_human_press_inside_echo_window_is_swallowed(self):
        """Documented residual risk of the 8 s window: a real human press on
        the SAME entity within 8 s of an app command is suppressed. The user's
        next press (outside the window) latches normally."""
        app = make_app(context_uid=HUMAN_ID)
        app._note_app_light_command(LIGHT)
        self.clock.now = 104.0  # inside the window
        fire(app)
        self.assertFalse(app._sleep_activated_during_presence)

        self.clock.now = 100.0 + ECHO_WINDOW_S + 1.0
        fire(app)
        self.assertTrue(app._sleep_activated_during_presence)


class YamlKnobs(unittest.TestCase):
    def test_yaml_carries_new_window_and_service_account_id(self):
        text = (_LIGHTS_DIR / "family_room_lights.yaml").read_text()
        self.assertRegex(text, r"manual_bright_echo_suppress_seconds:\s*8\b")
        # Never hardcode the id in .py - it must live in yaml as a knob.
        self.assertRegex(text, r'appdaemon_user_id:\s*"[0-9a-f]{32}"')

    def test_id_not_hardcoded_in_module(self):
        source = (_LIGHTS_DIR / "family_room_lights.py").read_text()
        yaml_text = (_LIGHTS_DIR / "family_room_lights.yaml").read_text()
        m = re.search(r'appdaemon_user_id:\s*"([0-9a-f]{32})"', yaml_text)
        self.assertIsNotNone(m)
        self.assertNotIn(m.group(1), source)


if __name__ == "__main__":
    unittest.main()
