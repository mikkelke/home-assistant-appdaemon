from __future__ import annotations

import asyncio
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

import weather_opening_alert as woa  # noqa: E402


VALID_ARGS = {
    "openings": [
        {"entity_id": "cover.rooftop_hatch", "bearing": 0, "rooftop": True, "area": "rooftop"},
        {"entity_id": "cover.living_room_window", "bearing": 90, "rooftop": False, "area": "living_room"},
    ],
}


def make_app(args):
    """WeatherOpeningAlert without running AppDaemon's __init__ - initialize() is
    exercised directly against a real (possibly minimal) args dict, with listen_state/
    run_every/run_in/log mocked so no real AppDaemon plumbing is required."""
    app = woa.WeatherOpeningAlert.__new__(woa.WeatherOpeningAlert)
    app.args = args
    app.listen_state = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.log = MagicMock()
    app.get_app = MagicMock(return_value=None)
    return app


class RunEveryRegistration(unittest.TestCase):
    """Regression test for the 2026-07-15 'now' vs 'immediate' scheduler bug (commit
    8666460): run_every(cb, "now", interval) fires the first call at now+interval, not
    immediately - only the literal string "immediate" does. Every reload silently left
    this app blind for up to 60s. Pins the fix so it can't silently regress."""

    def test_run_every_uses_immediate_with_60s_interval(self):
        app = make_app(VALID_ARGS)
        app.initialize()
        args, kwargs = app.run_every.call_args
        self.assertEqual(args[1], "immediate")
        self.assertNotEqual(args[1], "now")
        self.assertEqual(args[2], 60)


class OpeningsConfigGuard(unittest.TestCase):
    """openings is required config; without a non-empty list, initialize() must log an
    error and bail out before registering any listeners or the periodic evaluate tick -
    a misconfigured app should stay silent, not half-start and error on every callback."""

    def test_missing_openings_logs_error_and_skips_registration(self):
        app = make_app({})
        app.initialize()
        messages = [c.args[0] for c in app.log.call_args_list if c.args]
        self.assertTrue(any("openings must be a non-empty list" in m for m in messages))
        app.run_every.assert_not_called()
        app.listen_state.assert_not_called()

    def test_empty_openings_list_also_skips_registration(self):
        app = make_app({"openings": []})
        app.initialize()
        app.run_every.assert_not_called()


class EvalPriority(unittest.TestCase):
    """Priority arbitration between the two alert channels - rooftop rain always wins
    over a window-rain condition, matching physical severity (an open rooftop door in
    rain is worse than a window catching wind-blown rain)."""

    def _app(self):
        return woa.WeatherOpeningAlert.__new__(woa.WeatherOpeningAlert)

    def test_rooftop_wins_when_both_active(self):
        app = self._app()
        result = app._eval_priority(
            {"active": True, "reason": "roof", "target_area": "rooftop"},
            {"active": True, "reason": "window", "target_area": "living_room"},
        )
        self.assertEqual(result["priority"], "rooftop_rain")
        self.assertEqual(result["target_area"], "rooftop")

    def test_window_wins_when_rooftop_inactive(self):
        app = self._app()
        result = app._eval_priority(
            {"active": False, "reason": "", "target_area": ""},
            {"active": True, "reason": "window", "target_area": "living_room"},
        )
        self.assertEqual(result["priority"], "window_rain")
        self.assertEqual(result["target_area"], "living_room")

    def test_none_when_neither_active(self):
        app = self._app()
        result = app._eval_priority(
            {"active": False, "reason": "", "target_area": ""},
            {"active": False, "reason": "", "target_area": ""},
        )
        self.assertEqual(result, {"priority": "none", "reason": "", "target_area": ""})


class WindInBand(unittest.TestCase):
    """Wind-direction matching against a window's compass bearing, wrap-safe across the
    0/360 seam (a window facing near-north must not be missed just because the raw
    bearing-minus-wind subtraction goes negative)."""

    def test_wind_on_bearing(self):
        self.assertTrue(woa.wind_in_band(90, 90, 30))

    def test_wind_within_band(self):
        self.assertTrue(woa.wind_in_band(110, 90, 30))

    def test_wind_at_band_edge_inclusive(self):
        self.assertTrue(woa.wind_in_band(120, 90, 30))

    def test_wind_just_outside_band(self):
        self.assertFalse(woa.wind_in_band(121, 90, 30))

    def test_wraparound_across_zero(self):
        # bearing near 360, wind just past 0 - must still match via modulo, not a raw diff
        self.assertTrue(woa.wind_in_band(15, 350, 30))


class WindScalarToKmh(unittest.TestCase):
    """Unit conversion for wind speed/gust sensors that don't already report km/h -
    getting this wrong would silently corrupt every windy-threshold comparison."""

    def test_none_uom_assumed_kmh(self):
        self.assertEqual(woa._wind_scalar_to_kmh(25.0, None), 25.0)

    def test_meters_per_second(self):
        self.assertAlmostEqual(woa._wind_scalar_to_kmh(10.0, "m/s"), 36.0, places=3)

    def test_mph(self):
        self.assertAlmostEqual(woa._wind_scalar_to_kmh(10.0, "mph"), 16.0934, places=3)

    def test_knots(self):
        self.assertAlmostEqual(woa._wind_scalar_to_kmh(10.0, "kn"), 18.52, places=3)

    def test_already_kmh_unchanged(self):
        self.assertEqual(woa._wind_scalar_to_kmh(25.0, "km/h"), 25.0)

    def test_unrecognized_unit_falls_back_unchanged(self):
        self.assertEqual(woa._wind_scalar_to_kmh(25.0, "furlong/fortnight"), 25.0)


class AreaLabel(unittest.TestCase):
    def test_underscore_to_title_case(self):
        self.assertEqual(woa._area_label("living_room"), "Living Room")

    def test_empty_string(self):
        self.assertEqual(woa._area_label(""), "")


class RooftopPhrasing(unittest.TestCase):
    """Announcement copy names WHICH terrace door is open (user 2026-08-07), mirroring
    the dashboard's red alert row; grammar flexes for one vs both doors."""

    def test_single_named_door_tts(self):
        self.assertEqual(
            woa.rooftop_tts_message(["the living room terrace door"]),
            "Rain detected and the living room terrace door is open. Please close it.",
        )

    def test_single_named_door_push_capitalized(self):
        self.assertEqual(
            woa.rooftop_push_message(["the living room terrace door"]),
            "The living room terrace door is open. Please close it.",
        )

    def test_both_doors(self):
        self.assertEqual(
            woa.rooftop_tts_message(["the living room terrace door", "Claudia's terrace door"]),
            "Rain detected and both terrace doors are open. Please close them.",
        )

    def test_unnamed_door_falls_back(self):
        self.assertEqual(
            woa.rooftop_tts_message([""]),
            "Rain detected and a terrace door is open. Please close it.",
        )


class RooftopNotifyEdge(unittest.TestCase):
    """The announcement fires exactly once per rooftop episode: rising edge only,
    silent while latched (rain dips do not re-announce), re-armed once the alert
    clears so a genuinely new episode announces again."""

    def _app(self):
        app = woa.WeatherOpeningAlert.__new__(woa.WeatherOpeningAlert)
        app._rooftop_notified = False
        return app

    def test_fires_once_then_rearms_after_clear(self):
        app = self._app()
        self.assertTrue(app._rooftop_notify_due(True))
        self.assertFalse(app._rooftop_notify_due(True))
        self.assertFalse(app._rooftop_notify_due(False))
        self.assertTrue(app._rooftop_notify_due(True))

    def test_inactive_never_fires(self):
        app = self._app()
        self.assertFalse(app._rooftop_notify_due(False))
        self.assertFalse(app._rooftop_notify_due(False))


class RooftopNotifyWiring(unittest.TestCase):
    """initialize() must survive missing notifier apps (get_app -> None), and
    _notify_rooftop must log-and-skip in that state, never crash the evaluate loop."""

    def test_initialize_without_notifiers(self):
        app = make_app(VALID_ARGS)
        app.initialize()
        self.assertIsNone(app.sonos_notifier)
        self.assertIsNone(app.mobile_notifier)

    def test_notify_without_notifiers_does_not_raise(self):
        app = make_app(VALID_ARGS)
        app.initialize()
        asyncio.run(app._notify_rooftop({"open_names": ["the living room terrace door"]}))

    def test_notify_dispatches_sonos_and_push(self):
        app = make_app(VALID_ARGS)
        app.initialize()
        app.sonos_notifier = MagicMock()
        app.submit_to_executor = MagicMock()
        push_calls = []

        async def push(**kwargs):
            push_calls.append(kwargs)

        app.mobile_notifier = types.SimpleNamespace(notify=push)
        asyncio.run(app._notify_rooftop({"open_names": ["Claudia's terrace door"]}))

        app.submit_to_executor.assert_called_once()
        _, sonos_kwargs = app.submit_to_executor.call_args
        self.assertEqual(
            sonos_kwargs["message"],
            "Rain detected and Claudia's terrace door is open. Please close it.",
        )
        self.assertEqual(len(push_calls), 1)
        self.assertEqual(push_calls[0]["target"], "all")
        self.assertEqual(push_calls[0]["title"], "Rain on the roof")
        self.assertEqual(
            push_calls[0]["message"],
            "Claudia's terrace door is open. Please close it.",
        )


if __name__ == "__main__":
    unittest.main()
