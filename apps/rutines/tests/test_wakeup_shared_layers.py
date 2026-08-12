"""The seam between wakeup_bedroom and the shared layers it now orchestrates.

wakeup_bedroom used to own two things it had no business owning: how the bedroom
blind gets positioned, and how the bedroom gets lit. Those moved to
``apps/blinds/blind_arbiter.py`` and ``apps/lights/light_ramp.py``.

This file pins the two properties that matter for an alarm that wakes a real person:

1. **Fail-safe degradation.** A shared module that fails to import must cost the house
   a layer, never the wake-up. Every test here that sets the module to ``None``
   asserts the routine still does exactly what it did before the split.
2. **The two 2026-08-12 fixes survive the move.** The venting blind target and the
   re-checked daylight abort are pinned in test_wakeup_heat / test_wakeup_ramp_handoff
   against the app's own methods; here they are pinned again *through* the new seams,
   including the degraded path where the shared modules are gone.
"""

from __future__ import annotations

import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

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

import wakeup_bedroom as wb  # noqa: E402

NOW = datetime(2026, 8, 12, 5, 50, 0)
COVER = "cover.bedroom_blind"
LIGHT = "light.bedroom_bed_lights"
AL_BRIGHTNESS = "switch.adaptive_lighting_adapt_brightness_bedroom_bed_lights"

# Sun beating on the ENE window: every automatic heat reason resolves to 72 on its own.
SUN_ON_WINDOW = {
    ("sun.sun", "azimuth"): 70.0,
    ("sun.sun", "elevation"): 20.0,
    ("sensor.gw2000a_solar_radiation", None): 600.0,
    ("sensor.gw2000a_outdoor_temperature", None): 18.0,
}


@contextmanager
def shared_layer_missing(*names):
    """Simulate ``import blind_arbiter`` / ``import light_ramp`` having failed."""
    saved = {n: getattr(wb, n) for n in names}
    try:
        for n in names:
            setattr(wb, n, None)
        yield
    finally:
        for n, mod in saved.items():
            setattr(wb, n, mod)


def _bare_app():
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.user_log = "test_log"
    app.log_lines = []
    app.log = lambda msg, **kw: app.log_lines.append(msg)
    app.call_service = MagicMock()
    return app


def heat_app(states, *, heat_wave_on=False, sleep_plan=None):
    """Just enough state for _decide_bedroom_wake_target, mirroring test_wakeup_heat."""
    app = _bare_app()
    app.bedroom_cover_target = 38
    app.bedroom_cover_target_heat_wave = 72
    app.heat_wave_entity = "input_boolean.heat_wave_mode"
    app.heat_auto_enable = True
    app.heat_wave_manual_auto_clear = True
    app.sun_entity = "sun.sun"
    app.solar_radiation_entity = "sensor.gw2000a_solar_radiation"
    app.window_azimuth = 70.0
    app.az_tolerance = 55.0
    app.min_elevation = 3.0
    app.radiation_threshold = 250.0
    app.outdoor_temp_entity = "sensor.gw2000a_outdoor_temperature"
    app.live_outdoor_hot_c = 22.0
    app.forecast_high_threshold_c = 25.0
    app.forecast_max_age_min = 180
    app._forecast_high_c = None
    app._forecast_high_at = None
    app.sleep_plan_entity = "sensor.sleep_plan"
    app.vent_plan_states = {"windows", "nothing"}
    app.datetime = lambda: NOW

    all_states = dict(states)
    all_states[(app.heat_wave_entity, None)] = "on" if heat_wave_on else "off"
    all_states[(app.sleep_plan_entity, None)] = sleep_plan
    app.get_state = lambda entity, **kw: all_states.get((entity, kw.get("attribute")))
    app.turn_off = MagicMock()
    return app


def ramp_app(*, current_pct=50, adaptive_target=None, session="on", dark=True):
    app = _bare_app()
    app.light_entity = LIGHT
    app.adaptive_brightness_switch = AL_BRIGHTNESS
    app.adaptive_main_switch = "switch.adaptive_lighting_bedroom"
    app.bed_session_entity = "input_boolean.bedroom_bed_session"
    app.ramp_active = True
    app.ramp_timer = None
    app.current_pct = current_pct
    app.ramp_start_pct = 1
    app.ramp_step_pct = 6
    app.ramp_interval_sec = 60
    app.ramp_max_pct = 90
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    app._schedule_next_ramp_tick = MagicMock()
    app._finish_ramp = MagicMock()
    app._read_adaptive_target_pct = lambda: adaptive_target
    app._room_dark_for_wake_light = lambda: dark
    app.get_state = lambda entity, **kw: session
    return app


class BlindWriteGoesThroughTheOwner(unittest.TestCase):
    EXPECTED = ("cover/set_cover_position", {"entity_id": COVER, "position": 38})

    def _set(self, app):
        app._set_cover_position(COVER, 38)
        return (app.call_service.call_args.args[0], app.call_service.call_args.kwargs)

    def test_routed_through_the_arbiter(self):
        app = _bare_app()
        self.assertEqual(self._set(app), self.EXPECTED)

    def test_identical_call_when_the_arbiter_is_missing(self):
        app = _bare_app()
        with shared_layer_missing("blind_arbiter"):
            self.assertEqual(self._set(app), self.EXPECTED)

    def test_the_wake_write_is_never_dropped(self):
        """The light ramp waits for this cover to REACH its target before it starts, so a
        write silently skipped here strands the whole morning - blind shut, no ramp."""
        app = _bare_app()
        app._set_cover_position(COVER, 72)
        app.call_service.assert_called_once()


class VentingFixSurvivesTheMove(unittest.TestCase):
    """2026-08-12: the heat-block position (72) leaves the openable pane covered, so on a
    night the plan is to VENT the blind must land on the default (38) instead. That rule
    is now expressed as the arbiter's vent > wake rank - it must hold either way."""

    def test_venting_beats_heat_through_the_arbiter(self):
        app = heat_app(SUN_ON_WINDOW, sleep_plan="windows")
        target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 38)
        self.assertIn("venting tonight", reason)
        self.assertIn("sun on window", reason)  # the overridden reason is preserved

    def test_venting_still_beats_heat_with_no_arbiter_at_all(self):
        """The fail-safe that matters most: losing the shared layer must not hand back a
        blind position that makes the window physically unopenable."""
        app = heat_app(SUN_ON_WINDOW, sleep_plan="windows")
        with shared_layer_missing("blind_arbiter"):
            target, reason = app._decide_bedroom_wake_target()
        self.assertEqual(target, 38)
        self.assertIn("venting tonight", reason)

    def test_manual_force_yields_to_venting_and_is_still_consumed(self):
        for missing in (False, True):
            with self.subTest(arbiter_missing=missing):
                app = heat_app(SUN_ON_WINDOW, heat_wave_on=True, sleep_plan="nothing")
                if missing:
                    with shared_layer_missing("blind_arbiter"):
                        target, _ = app._decide_bedroom_wake_target()
                else:
                    target, _ = app._decide_bedroom_wake_target()
                self.assertEqual(target, 38)
                # A stale manual force must not survive to the next wake.
                app.turn_off.assert_called_once_with(app.heat_wave_entity)

    def test_ac_night_keeps_shading_either_way(self):
        for missing in (False, True):
            with self.subTest(arbiter_missing=missing):
                app = heat_app(SUN_ON_WINDOW, sleep_plan="ac")
                if missing:
                    with shared_layer_missing("blind_arbiter"):
                        self.assertEqual(app._decide_bedroom_wake_target()[0], 72)
                else:
                    self.assertEqual(app._decide_bedroom_wake_target()[0], 72)

    def test_an_arbiter_that_picks_nobody_falls_back_to_the_raw_target(self):
        """Defensive: arbitrate() returning None must never leave the caller with no
        position at all."""
        app = heat_app(SUN_ON_WINDOW, sleep_plan="windows")
        stub = types.SimpleNamespace(
            SOURCE_VENT="vent", SOURCE_WAKE="wake",
            BlindRequest=lambda *a, **kw: None,
            arbitrate=lambda requests: None,
        )
        saved = wb.blind_arbiter
        try:
            wb.blind_arbiter = stub
            target, reason = app._decide_bedroom_wake_target()
        finally:
            wb.blind_arbiter = saved
        self.assertEqual(target, 72)
        self.assertIn("sun on window", reason)


class LightRampGoesThroughTheSharedLayer(unittest.TestCase):
    EXPECTED = ("light/turn_on", {"entity_id": LIGHT, "brightness_pct": 56, "transition": 60})

    def _emit(self, app, pct):
        returned = app._set_light_pct(pct)
        call = (app.call_service.call_args.args[0], app.call_service.call_args.kwargs)
        return returned, call

    def test_routed_through_light_ramp(self):
        returned, call = self._emit(ramp_app(), 56)
        self.assertEqual(call, self.EXPECTED)
        self.assertEqual(returned, 56)

    def test_identical_call_when_light_ramp_is_missing(self):
        with shared_layer_missing("light_ramp"):
            returned, call = self._emit(ramp_app(), 56)
        self.assertEqual(call, self.EXPECTED)
        self.assertEqual(returned, 56)

    def test_target_caps_at_the_ramp_ceiling_either_way(self):
        self.assertEqual(ramp_app(adaptive_target=100)._ramp_target_pct(), 90)
        self.assertEqual(ramp_app(adaptive_target=45)._ramp_target_pct(), 45)
        self.assertEqual(ramp_app(adaptive_target=None)._ramp_target_pct(), 90)
        with shared_layer_missing("light_ramp"):
            self.assertEqual(ramp_app(adaptive_target=100)._ramp_target_pct(), 90)
            self.assertEqual(ramp_app(adaptive_target=45)._ramp_target_pct(), 45)
            self.assertEqual(ramp_app(adaptive_target=None)._ramp_target_pct(), 90)

    def test_step_and_start_match_the_pre_split_arithmetic(self):
        app = ramp_app(current_pct=50)
        self.assertEqual(app._ramp_next_pct(90), 56)
        self.assertEqual(app._ramp_start_pct(), 1)
        with shared_layer_missing("light_ramp"):
            self.assertEqual(app._ramp_next_pct(90), 56)
            self.assertEqual(app._ramp_start_pct(), 1)

    def test_ramp_tick_still_steps_without_the_shared_layer(self):
        with shared_layer_missing("light_ramp"):
            app = ramp_app(current_pct=50)
            app._ramp_tick(None)
        app.call_service.assert_called_once_with(
            "light/turn_on", entity_id=LIGHT, brightness_pct=56, transition=60
        )
        self.assertEqual(app.current_pct, 56)
        app._schedule_next_ramp_tick.assert_called_once()


class DaylightAbortSurvivesTheMove(unittest.TestCase):
    """2026-08-12: the brightness check only ran once, right after the blind moved, so a
    ramp that started at 05:50 in a dim room drove on to 90 % by 06:06 while the sun came
    up. The tick has to re-ask - with or without the shared lighting layer."""

    def test_bright_room_finishes_the_ramp(self):
        for missing in (False, True):
            with self.subTest(light_ramp_missing=missing):
                app = ramp_app(current_pct=30, dark=False)
                if missing:
                    with shared_layer_missing("light_ramp"):
                        app._ramp_tick(None)
                else:
                    app._ramp_tick(None)
                app._finish_ramp.assert_called_once()
                self.assertIn("daylight", app._finish_ramp.call_args.args[0])
                app.call_service.assert_not_called()
                app._schedule_next_ramp_tick.assert_not_called()

    def test_dark_room_keeps_ramping(self):
        for missing in (False, True):
            with self.subTest(light_ramp_missing=missing):
                app = ramp_app(current_pct=30, dark=True)
                if missing:
                    with shared_layer_missing("light_ramp"):
                        app._ramp_tick(None)
                else:
                    app._ramp_tick(None)
                app._finish_ramp.assert_not_called()
                app.call_service.assert_called_once()

    def test_bed_session_still_outranks_the_daylight_reason(self):
        app = ramp_app(current_pct=30, dark=False, session="off")
        app._ramp_tick(None)
        app._finish_ramp.assert_called_once()
        self.assertIn("bed session", app._finish_ramp.call_args.args[0])


class AdaptiveLightingHandover(unittest.TestCase):
    def test_resume_turns_the_switch_back_on_either_way(self):
        for missing in (False, True):
            with self.subTest(light_ramp_missing=missing):
                app = ramp_app()
                if missing:
                    with shared_layer_missing("light_ramp"):
                        app._resume_adaptive_brightness()
                else:
                    app._resume_adaptive_brightness()
                app.turn_on.assert_called_once_with(AL_BRIGHTNESS)
                self.assertTrue(
                    any("Restoring Adaptive Lighting" in line for line in app.log_lines),
                    app.log_lines,
                )

    def test_resume_never_raises(self):
        """Teardown path: an exception here strands the switch off with nothing left to
        turn it back on until the next restart's reconcile."""
        for missing in (False, True):
            with self.subTest(light_ramp_missing=missing):
                app = ramp_app()
                app.turn_on.side_effect = RuntimeError("boom")
                if missing:
                    with shared_layer_missing("light_ramp"):
                        app._resume_adaptive_brightness()
                else:
                    app._resume_adaptive_brightness()

    def test_stop_light_ramp_restores_adaptive_lighting(self):
        app = ramp_app()
        app._session_listener = None
        app._stop_light_ramp()
        self.assertFalse(app.ramp_active)
        app.turn_on.assert_called_once_with(AL_BRIGHTNESS)


if __name__ == "__main__":
    unittest.main()
