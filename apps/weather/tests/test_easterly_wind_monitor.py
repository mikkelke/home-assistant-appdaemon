from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

import easterly_wind_monitor as ewm  # noqa: E402


def _states_getter(states):
    async def get_state(entity, **kw):
        return states.get(entity)
    return get_state


def make_app(states=None, **overrides):
    """EasterlyWindMonitor without running AppDaemon's initialize() - thresholds and
    counters are set directly so _check_conditions can be exercised in isolation, with
    get_state faked from a plain dict and call_service captured via AsyncMock."""
    app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
    app.wind_dir = "sensor.gw2000a_wind_direction"
    # Mirrors initialize(): unset means "instantaneous vane only", the pre-2026-08-12
    # behaviour every test below was written against. WindDirectionSource opts in.
    app.wind_dir_mean = overrides.get("wind_dir_mean", None)
    app.wind_gust = "sensor.gw2000a_wind_gust"
    app.wind_speed = "sensor.gw2000a_wind_speed"
    app.episode_entity = "input_boolean.easterly_wind_episode_active"
    app.dir_min = overrides.get("dir_min", 60.0)
    app.dir_max = overrides.get("dir_max", 120.0)
    app.wind_speed_windy = overrides.get("wind_speed_windy", 28.8)
    app.gust_windy = overrides.get("gust_windy", 54.0)
    app.wind_unit_label = overrides.get("wind_unit_label", "km/h")
    app.sustained_min = overrides.get("sustained_min", 5)
    app.end_after_min = overrides.get("end_after_min", 10)
    app.notify_target = "mikkel"
    app.notify_on_end = overrides.get("notify_on_end", False)
    app._in_episode = overrides.get("in_episode", False)
    app._condition_met_count = overrides.get("condition_met_count", 0)
    app._condition_not_met_count = overrides.get("condition_not_met_count", 0)
    app._last_gust_in_episode = overrides.get("last_gust_in_episode", 0.0)
    # Default True: in_episode=True here stands for an episode this instance started.
    # A rehydrated one (helper ON at startup, peak before the restart unknown) passes False.
    app._peak_from_episode_start = overrides.get("peak_from_episode_start", True)
    app.mobile_notifier = overrides.get("mobile_notifier", None)
    app.get_state = _states_getter(states or {})
    app.call_service = AsyncMock()
    app.log = MagicMock()
    return app


# direction in-band (60-120), gust over the default 54 km/h windy threshold
WINDY_STATES = {
    "sensor.gw2000a_wind_direction": "90",
    "sensor.gw2000a_wind_gust": "60",
    "sensor.gw2000a_wind_speed": "10",
}

# direction in-band but neither gust nor mean speed clears the windy threshold
CALM_STATES = {
    "sensor.gw2000a_wind_direction": "90",
    "sensor.gw2000a_wind_gust": "10",
    "sensor.gw2000a_wind_speed": "5",
}


class RunEveryRegistration(unittest.TestCase):
    """Regression test for the 2026-07-15 'now' vs 'immediate' scheduler bug (commit
    8666460): run_every(cb, "now", interval) fires the first call at now+interval, not
    immediately - only the literal string "immediate" does. Pins the fix so it can't
    silently regress."""

    def test_run_every_uses_immediate_with_configured_interval(self):
        app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
        app.args = {}
        app.get_app = MagicMock(return_value=None)
        app.run_every = MagicMock()
        app.run_in = MagicMock()
        app.log = MagicMock()
        app.initialize()
        args, kwargs = app.run_every.call_args
        self.assertEqual(args[1], "immediate")
        self.assertNotEqual(args[1], "now")
        self.assertEqual(args[2], 60)  # default check_interval_seconds


class ConditionThresholds(unittest.IsolatedAsyncioTestCase):
    """Direction-band and windy (gust-or-mean) threshold evaluation, independent of the
    sustained-count state machine (each test starts from a fresh, non-episode state)."""

    async def test_windy_and_in_band_counts_toward_sustain(self):
        app = make_app(WINDY_STATES)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 1)
        self.assertFalse(app._in_episode)

    async def test_calm_does_not_count(self):
        app = make_app(CALM_STATES)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 0)

    async def test_direction_outside_band_does_not_count_even_if_windy(self):
        states = dict(WINDY_STATES, **{"sensor.gw2000a_wind_direction": "200"})
        app = make_app(states)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 0)

    async def test_gust_alone_qualifies_without_mean_speed(self):
        states = {
            "sensor.gw2000a_wind_direction": "90",
            "sensor.gw2000a_wind_gust": "60",
            "sensor.gw2000a_wind_speed": "unknown",
        }
        app = make_app(states)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 1)

    async def test_unavailable_direction_is_treated_as_not_met(self):
        states = dict(WINDY_STATES, **{"sensor.gw2000a_wind_direction": "unavailable"})
        app = make_app(states)
        await app._check_conditions({})
        self.assertEqual(app._condition_met_count, 0)
        self.assertEqual(app._condition_not_met_count, 1)


class EpisodeLifecycle(unittest.IsolatedAsyncioTestCase):
    """The sustained-count state machine: an episode starts only after sustained_minutes
    consecutive windy+in-band checks, and ends only after end_after_minutes_not_met
    consecutive checks below threshold - single-tick blips must not flip either state."""

    async def test_episode_starts_at_sustained_threshold(self):
        app = make_app(WINDY_STATES, sustained_min=3, condition_met_count=2)
        await app._check_conditions({})
        self.assertTrue(app._in_episode)
        app.call_service.assert_awaited_once_with(
            "input_boolean/turn_on", entity_id=app.episode_entity,
        )

    async def test_episode_does_not_start_before_threshold(self):
        app = make_app(WINDY_STATES, sustained_min=3, condition_met_count=1)
        await app._check_conditions({})
        self.assertFalse(app._in_episode)
        app.call_service.assert_not_awaited()

    async def test_single_calm_tick_during_episode_does_not_end_it(self):
        app = make_app(
            CALM_STATES, in_episode=True, end_after_min=10, condition_not_met_count=0,
        )
        await app._check_conditions({})
        self.assertTrue(app._in_episode)
        app.call_service.assert_not_awaited()

    async def test_episode_ends_after_sustained_calm(self):
        app = make_app(
            CALM_STATES, in_episode=True, end_after_min=3, condition_not_met_count=2,
        )
        await app._check_conditions({})
        self.assertFalse(app._in_episode)
        app.call_service.assert_awaited_once_with(
            "input_boolean/turn_off", entity_id=app.episode_entity,
        )

    async def test_windy_tick_during_episode_tracks_peak_gust_not_restart(self):
        app = make_app(WINDY_STATES, in_episode=True, last_gust_in_episode=40.0)
        await app._check_conditions({})
        self.assertEqual(app._last_gust_in_episode, 60.0)
        app.call_service.assert_not_awaited()


class EpisodeEndPeakReporting(unittest.IsolatedAsyncioTestCase):
    """The end message's gust figure. An episode this instance started has a real peak; a
    rehydrated one (helper survived, the pre-restart peak did not) knows only what has been
    measured since the restart - and possibly nothing at all, which must never be reported
    as a peak of 0."""

    @staticmethod
    def _notifier():
        return MagicMock(notify=AsyncMock())

    @staticmethod
    def _end_log(app):
        return [c.args[0] for c in app.log.call_args_list if "Episode END" in str(c.args[0])][0]

    async def test_own_episode_reports_the_measured_peak(self):
        notifier = self._notifier()
        app = make_app(
            CALM_STATES, in_episode=True, end_after_min=1, last_gust_in_episode=62.0,
            notify_on_end=True, mobile_notifier=notifier,
        )
        await app._check_conditions({})
        self.assertFalse(app._in_episode)
        self.assertIn("Max gust was 62 km/h.", notifier.notify.await_args.kwargs["message"])
        self.assertIn("max gust in episode: 62.0 km/h", self._end_log(app))

    async def test_rehydrated_episode_without_a_windy_reading_reports_unknown_not_zero(self):
        notifier = self._notifier()
        app = make_app(
            CALM_STATES, in_episode=True, peak_from_episode_start=False, end_after_min=1,
            notify_on_end=True, mobile_notifier=notifier,
        )
        await app._check_conditions({})
        self.assertFalse(app._in_episode)

        message = notifier.notify.await_args.kwargs["message"]
        self.assertIn("unknown", message.lower())
        self.assertNotIn("0 km/h", message)
        end_log = self._end_log(app)
        self.assertIn("unknown", end_log.lower())
        self.assertNotIn("0.0 km/h", end_log)

    async def test_rehydrated_episode_qualifies_a_post_restart_peak(self):
        notifier = self._notifier()
        app = make_app(
            CALM_STATES, in_episode=True, peak_from_episode_start=False, end_after_min=1,
            last_gust_in_episode=45.0, notify_on_end=True, mobile_notifier=notifier,
        )
        await app._check_conditions({})

        message = notifier.notify.await_args.kwargs["message"]
        self.assertIn("45 km/h", message)
        self.assertIn("since AppDaemon restarted", message)
        self.assertIn("since restart: 45.0 km/h", self._end_log(app))

    async def test_the_flag_is_reset_for_the_next_episode(self):
        app = make_app(CALM_STATES, in_episode=True, peak_from_episode_start=False, end_after_min=1)
        await app._check_conditions({})
        self.assertTrue(app._peak_from_episode_start)
        self.assertEqual(app._last_gust_in_episode, 0.0)

    async def test_a_started_episode_owns_its_peak(self):
        app = make_app(
            WINDY_STATES, sustained_min=1, peak_from_episode_start=False,  # stale from before
        )
        await app._check_conditions({})
        self.assertTrue(app._in_episode)
        self.assertTrue(app._peak_from_episode_start)
        self.assertEqual(app._last_gust_in_episode, 60.0)


class EpisodeConditionsNow(unittest.TestCase):
    """_episode_conditions_now: the synchronous, init-only rehydration check."""

    def _make(self, states, wind_dir_mean=None):
        app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
        app.wind_dir = "sensor.gw2000a_wind_direction"
        app.wind_dir_mean = wind_dir_mean   # mirrors initialize(); None = vane only
        app.wind_gust = "sensor.gw2000a_wind_gust"
        app.wind_speed = "sensor.gw2000a_wind_speed"
        app.dir_min = 60.0
        app.dir_max = 120.0
        app.wind_speed_windy = 28.8
        app.gust_windy = 54.0
        app.get_state = lambda entity, **kw: states.get(entity)
        app.log = MagicMock()
        return app

    def test_true_when_windy_and_in_band(self):
        app = self._make(WINDY_STATES)
        self.assertTrue(app._episode_conditions_now())

    def test_false_when_calm(self):
        app = self._make(CALM_STATES)
        self.assertFalse(app._episode_conditions_now())

    def test_none_when_direction_unavailable(self):
        app = self._make({
            "sensor.gw2000a_wind_direction": "unavailable",
            "sensor.gw2000a_wind_gust": "60",
        })
        self.assertIsNone(app._episode_conditions_now())

    def test_gust_alone_qualifies_without_mean_speed(self):
        app = self._make({
            "sensor.gw2000a_wind_direction": "90",
            "sensor.gw2000a_wind_gust": "60",
            "sensor.gw2000a_wind_speed": "unknown",
        })
        self.assertTrue(app._episode_conditions_now())


class RestartRehydration(unittest.TestCase):
    """2026-07-27: the episode helper is an HA input_boolean and survives an AD restart
    even though _in_episode/the counters (in-memory) do not. initialize() seeds
    _in_episode from the helper's current state and, if the helper is ON but wind has
    already died down, immediately schedules the normal episode-end path instead of
    leaving the helper stuck for another end_after_minutes_not_met debounce."""

    def _make(self, states, args=None):
        app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
        app.args = dict(args or {})
        app.get_state = lambda entity, **kw: states.get(entity)
        app.get_app = MagicMock(return_value=None)
        app.listen_state = MagicMock()
        app.run_every = MagicMock()
        app.run_in_calls = []
        app.run_in = lambda cb, delay, **kw: app.run_in_calls.append((cb, delay))
        app.create_task_calls = []
        app.create_task = lambda coro: app.create_task_calls.append(coro) or coro
        app.log = MagicMock()
        app.initialize()
        return app

    def test_helper_off_at_startup_seeds_not_in_episode(self):
        app = self._make({"input_boolean.easterly_wind_episode_active": "off"})
        self.assertFalse(app._in_episode)
        self.assertTrue(app._peak_from_episode_start)  # next episode starts here
        # Only the pre-existing _check_episode_entity_exists run_in - no forced end.
        self.assertEqual(len(app.run_in_calls), 1)

    def test_helper_missing_entirely_seeds_not_in_episode(self):
        app = self._make({})  # get_state -> None for the helper (never created yet)
        self.assertFalse(app._in_episode)

    def test_helper_on_and_still_windy_stays_in_episode_without_forcing_an_end(self):
        states = dict(WINDY_STATES, **{"input_boolean.easterly_wind_episode_active": "on"})
        app = self._make(states)
        self.assertTrue(app._in_episode)
        # The pre-restart peak is gone with the process - anything measured from here on
        # covers only part of the episode.
        self.assertFalse(app._peak_from_episode_start)
        self.assertEqual(len(app.run_in_calls), 1)  # no immediate-end scheduled

    def test_helper_on_but_unavailable_reading_does_not_force_an_end(self):
        """Inconclusive (unavailable) data at the exact restart instant must not force-end
        a possibly-real ongoing episode - only a DEFINITE calm reading does."""
        states = {
            "input_boolean.easterly_wind_episode_active": "on",
            "sensor.gw2000a_wind_direction": "unavailable",
            "sensor.gw2000a_wind_gust": "unavailable",
        }
        app = self._make(states)
        self.assertTrue(app._in_episode)
        self.assertEqual(len(app.run_in_calls), 1)

    def test_helper_on_but_calm_schedules_the_normal_end_path(self):
        states = dict(CALM_STATES, **{"input_boolean.easterly_wind_episode_active": "on"})
        app = self._make(states)

        self.assertTrue(app._in_episode)  # rehydrated first...
        self.assertEqual(app._condition_not_met_count, app.end_after_min)  # ...gate primed...
        self.assertEqual(len(app.run_in_calls), 2)  # ...then deferred one tick
        cb, delay = app.run_in_calls[-1]
        self.assertEqual(delay, 0)

        cb({})  # simulate AppDaemon firing the deferred callback

        self.assertEqual(len(app.create_task_calls), 1)
        coro = app.create_task_calls[0]
        self.assertTrue(asyncio.iscoroutine(coro))
        self.assertEqual(coro.cr_code.co_name, "_maybe_end_episode")
        coro.close()


class RestartRehydrationEndToEnd(unittest.IsolatedAsyncioTestCase):
    """Same scenario as RestartRehydration.test_helper_on_but_calm_schedules_the_normal_
    end_path, but actually runs the scheduled coroutine to confirm the observable effect:
    the helper turns off (no duplicate/stuck-on state)."""

    async def test_calm_at_restart_actually_turns_the_helper_off(self):
        states = dict(CALM_STATES, **{"input_boolean.easterly_wind_episode_active": "on"})
        app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
        app.args = {}
        app.get_state = lambda entity, **kw: states.get(entity)
        app.get_app = MagicMock(return_value=None)
        app.listen_state = MagicMock()
        app.run_every = MagicMock()
        app.run_in_calls = []
        app.run_in = lambda cb, delay, **kw: app.run_in_calls.append((cb, delay))
        app.call_service = AsyncMock()
        app.log = MagicMock()

        task_holder = {}

        def create_task(coro):
            task_holder["task"] = asyncio.ensure_future(coro)
            return task_holder["task"]

        app.create_task = create_task

        app.initialize()
        self.assertTrue(app._in_episode)

        cb, _delay = app.run_in_calls[-1]
        cb({})
        await task_holder["task"]

        self.assertFalse(app._in_episode)
        app.call_service.assert_awaited_once_with(
            "input_boolean/turn_off", entity_id=app.episode_entity,
        )


MEAN_ENT = "sensor.gw2000a_wind_direction_10m_avg"


class WindDirectionSource(unittest.TestCase):
    """"Easterly wind" means the MEAN flow is easterly, so the 60-120 deg test runs on the
    station's 10-minute mean, not the instantaneous vane (2026-08-12).

    WHY IT MATTERS: this app's whole premise is a SUSTAINED pattern - it wants the condition
    on 5 consecutive checks before it will tell Mikkel the building is under easterly load,
    and any single sample that falls out of the band resets _condition_met_count to zero. The
    vane cannot carry that: measured over the 60 days to 2026-08-12 it sat a median 26 deg
    from the station's own 10-minute mean (p90 114 deg), and given it is inside the 60-120 deg
    band now it is still inside it four minutes later only 65% of the time.

    Replayed against the recorder over the windy months 2026-01-01..2026-03-01, there were 89
    minutes in which the 10-minute mean said easterly-and-windy while the vane's instantaneous
    reading knocked the run back to zero. On the vane the app would have raised 4 episodes
    totalling 1.0 h; on the mean, 6 episodes totalling 2.2 h. The two extra are real
    building-load events that were never reported - 2026-02-16 11:54-12:36 (42 minutes) in
    full, and 2026-02-01's episode started 14 minutes late."""

    def test_mean_is_preferred_over_the_vane(self):
        self.assertEqual(ewm.EasterlyWindMonitor._pick_direction("95", "300"), (95.0, "mean"))

    def test_vane_excursion_no_longer_breaks_a_sustained_easterly(self):
        """The concrete failure mode: mean flow easterly at 95 deg, vane momentarily at
        300 deg. On the vane that check fails the band test and the 5-check run restarts."""
        app = make_app({
            "sensor.gw2000a_wind_direction": "300",
            MEAN_ENT: "95",
            "sensor.gw2000a_wind_gust": "60",
            "sensor.gw2000a_wind_speed": "30",
        }, wind_dir_mean=MEAN_ENT, condition_met_count=4)
        asyncio.run(app._check_conditions({}))
        self.assertTrue(app._in_episode, "a single vane excursion cancelled a real episode")

    def test_the_same_reading_on_the_vane_alone_cancels_the_run(self):
        """Same data, mean not configured: this is what the app used to do, and why the
        2026-02-16 easterly was never reported."""
        app = make_app({
            "sensor.gw2000a_wind_direction": "300",
            MEAN_ENT: "95",
            "sensor.gw2000a_wind_gust": "60",
            "sensor.gw2000a_wind_speed": "30",
        }, condition_met_count=4)
        asyncio.run(app._check_conditions({}))
        self.assertFalse(app._in_episode)
        self.assertEqual(app._condition_met_count, 0, "the run must have been reset")

    def test_falls_back_to_the_vane_when_the_mean_is_unavailable(self):
        self.assertEqual(
            ewm.EasterlyWindMonitor._pick_direction("unavailable", "95"), (95.0, "vane"))

    def test_falls_back_when_the_mean_is_absent(self):
        self.assertEqual(ewm.EasterlyWindMonitor._pick_direction(None, "95"), (95.0, "vane"))

    def test_both_unusable_is_inconclusive_not_zero_degrees(self):
        """Returning 0.0 would read as "north" and quietly end episodes; the caller needs
        None so it keeps its existing inconclusive handling."""
        self.assertEqual(
            ewm.EasterlyWindMonitor._pick_direction("unknown", "unavailable"), (None, None))
        self.assertEqual(ewm.EasterlyWindMonitor._pick_direction("n/a", "junk"), (None, None))

    def test_rehydration_check_uses_the_mean_too(self):
        """_episode_conditions_now decides at startup whether a helper left ON should be
        closed out immediately. Reading a different instrument there than the running loop
        does would make the restart path disagree with itself."""
        app = ewm.EasterlyWindMonitor.__new__(ewm.EasterlyWindMonitor)
        app.wind_dir = "sensor.gw2000a_wind_direction"
        app.wind_dir_mean = MEAN_ENT
        app.wind_gust = "sensor.gw2000a_wind_gust"
        app.wind_speed = "sensor.gw2000a_wind_speed"
        app.dir_min, app.dir_max = 60.0, 120.0
        app.wind_speed_windy, app.gust_windy = 28.8, 54.0
        states = {
            "sensor.gw2000a_wind_direction": "300",
            MEAN_ENT: "95",
            "sensor.gw2000a_wind_gust": "60",
            "sensor.gw2000a_wind_speed": "30",
        }
        app.get_state = lambda entity, **kw: states.get(entity)
        app.log = MagicMock()
        self.assertTrue(app._episode_conditions_now(),
                        "the vane's excursion would have force-ended a live episode")


if __name__ == "__main__":
    unittest.main()
