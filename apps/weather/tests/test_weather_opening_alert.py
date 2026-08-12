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


class FacadeWindDirectionSource(unittest.TestCase):
    """The +/-30 deg facade bands are a statement about the MEAN FLOW, so they must be tested
    against the station's 10-minute mean, not one sample of a turbulent vane (2026-08-12).

    WHY IT MATTERS: this app asks whether rain is being driven INTO a specific window, and
    then demands the answer hold unbroken for window_rain_sustain_minutes (4) - any single
    out-of-band sample pops _wind_ok_since and restarts the clock. Measured against the
    recorder over the 60 days to 2026-08-12, the instantaneous vane sits a median 26 deg from
    the station's own 10-minute mean, p90 114 deg - wider than a whole band. Given the vane is
    inside a band now, it is still inside it 4 minutes later only 21% of the time on the SSE
    facade (kitchen/living/dining) and 65% on the ENE one; the 10-minute mean manages 38% and
    85%. So the sustain filter was rejecting turbulence rather than weather.

    Replaying those 60 days against the recorder: on the mean the ENE facade accumulates 92
    qualifying minutes against the vane's 50, alerts land up to 12 minutes earlier (2026-07-26
    22:10 vs 22:22 local), and two episodes the vane never caught at all appear (2026-06-28
    07:28, 2026-07-20 08:14). It is not merely more sensitive - on the rarely-blowing SSE and
    NNW sectors the mean yields FEWER in-band minutes, because single-sample excursions into
    a sector the wind is not actually in stop counting."""

    MEAN = "sensor.gw2000a_wind_direction_10m_avg"

    def _app(self, mean_entity=MEAN, states=None):
        args = dict(VALID_ARGS)
        if mean_entity is not None:
            args["wind_direction_mean_entity"] = mean_entity
        app = make_app(args)
        app.initialize()
        app._fake_states = dict(states or {})

        async def get_state(entity, attribute=None, **kw):
            if attribute is not None:
                return None
            return app._fake_states.get(entity)

        app.get_state = get_state
        return app

    def test_mean_is_preferred_over_the_vane(self):
        """The station publishes both; the mean is the one that answers the question."""
        app = self._app(states={
            "sensor.gw2000a_wind_direction": "20",
            self.MEAN: "160",
        })
        direction, source = asyncio.run(app._facade_wind_direction())
        self.assertEqual(direction, 160.0)
        self.assertEqual(source, "mean")

    def test_a_vane_excursion_no_longer_decides_the_facade(self):
        """The concrete failure: the mean flow is SSE (bearing 155, band 125-185) while the
        vane has swung to 20 deg. On the vane the living-room window drops out of band and
        the 4-minute sustain clock restarts; on the mean it keeps counting."""
        app = self._app(states={
            "sensor.gw2000a_wind_direction": "20",
            self.MEAN: "160",
        })
        direction, _ = asyncio.run(app._facade_wind_direction())
        self.assertTrue(woa.wind_in_band(direction, 155.0, 30.0))
        self.assertFalse(woa.wind_in_band(20.0, 155.0, 30.0),
                         "the vane sample alone would have cancelled the episode")

    def test_falls_back_to_the_vane_when_the_mean_is_unavailable(self):
        """The WS90 publishes both or neither, but a partial outage must not silence the
        alert - a noisy direction still beats no direction."""
        app = self._app(states={
            "sensor.gw2000a_wind_direction": "70",
            self.MEAN: "unavailable",
        })
        direction, source = asyncio.run(app._facade_wind_direction())
        self.assertEqual(direction, 70.0)
        self.assertEqual(source, "vane")

    def test_falls_back_when_the_mean_is_missing_entirely(self):
        app = self._app(states={"sensor.gw2000a_wind_direction": "245"})
        direction, source = asyncio.run(app._facade_wind_direction())
        self.assertEqual(direction, 245.0)
        self.assertEqual(source, "vane")

    def test_unconfigured_mean_keeps_the_legacy_vane_behaviour(self):
        """Removing the knob from the yaml must back the change out cleanly."""
        app = self._app(mean_entity=None, states={
            "sensor.gw2000a_wind_direction": "70",
            self.MEAN: "160",
        })
        direction, source = asyncio.run(app._facade_wind_direction())
        self.assertEqual(direction, 70.0)
        self.assertEqual(source, "vane")

    def test_both_unreadable_yields_no_direction(self):
        """wind_dir_ok then goes False and every window band is skipped - the app must never
        guess a direction it does not have."""
        app = self._app(states={
            "sensor.gw2000a_wind_direction": "unknown",
            self.MEAN: "unavailable",
        })
        direction, _ = asyncio.run(app._facade_wind_direction())
        self.assertIsNone(direction)

    def test_the_mean_entity_is_listened_to(self):
        """Without a listener the app would only re-evaluate on the vane's ticks, and the
        mean updates on its own schedule (median 60 s, p99 600 s)."""
        args = dict(VALID_ARGS)
        args["wind_direction_mean_entity"] = self.MEAN
        app = make_app(args)
        app.initialize()
        listened = [c.args[1] for c in app.listen_state.call_args_list]
        self.assertIn(self.MEAN, listened)

    def test_no_listener_when_the_mean_is_not_configured(self):
        app = make_app(VALID_ARGS)
        app.initialize()
        listened = [c.args[1] for c in app.listen_state.call_args_list]
        self.assertNotIn(self.MEAN, listened)


class RainGateUsesTheRateNotTheContact(unittest.TestCase):
    """This app was already right about rain, and must stay right: it gates on the piezo RATE.

    binary_sensor.gw2000a_rain_state_piezo is an impact detector, not a rain sensor - over the
    60 days to 2026-08-12 it reported 375 episodes / 142 h of "rain", 298 of which produced
    0.0 mm/h and never moved the accumulator by a single 0.1 mm tick. Terrace door 1 alone
    stood open for 1533 of those contact-on minutes. Had this app believed the contact, 1467
    of them would have announced "rain on the roof" over Sonos and pushed every phone in the
    house, for weather that was not happening.

    The 0.5 mm/h bar is "any measurable rain", not "moderate rain": the rate quantises in
    0.6 mm/h steps, so the smallest non-zero reading the sensor can produce already clears it."""

    def test_the_configured_rain_entity_is_the_rate_sensor(self):
        app = make_app(VALID_ARGS)
        app.initialize()
        self.assertEqual(app.rain_entity, "sensor.gw2000a_rain_rate_piezo")
        self.assertNotIn("rain_state", app.rain_entity)

    def test_the_smallest_reportable_rate_clears_the_bar(self):
        app = make_app(VALID_ARGS)
        app.initialize()
        self.assertLessEqual(app.rain_min, 0.6)


if __name__ == "__main__":
    unittest.main()
