# tests/test_house_night_mode.py - HouseNightMode: the latched household night
# boolean. Encodes the 2026-08-07 04:14-04:44 incident as a regression: Mikkel's
# 30-minute trip out of bed mid-night must NOT clear house_night_mode (the sustained
# clear only counts minutes inside 04:30-12:00, so a 04:26 sleep-mode drop needs to
# hold until 04:45 - and he was back in bed at 04:44). Same __new__ +
# monkeypatched-callables harness as the other tests here.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import collections
import sys
import types
import unittest
from datetime import datetime, time as dtime, timedelta
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

import house_night_mode as hnm  # noqa: E402

MIKKEL = "input_boolean.mikkel_sleep_mode"
KRISTINE = "input_boolean.kristine_sleep_mode"
CLAUDIA = "input_boolean.claudia_sleep_mode"
HOUSE = "input_boolean.house_night_mode"


def make_app(states, *, now):
    app = hnm.HouseNightMode.__new__(hnm.HouseNightMode)
    app.house_entity = HOUSE
    app.sleep_entities = [MIKKEL, KRISTINE, CLAUDIA]
    app.mikkel_sleep_entity = MIKKEL
    app.latch_window = (dtime(20, 0), dtime(4, 0))
    app.clear_window = (dtime(4, 30), dtime(12, 0))
    app.clear_sustain_seconds = 15 * 60
    app.hard_clear_time = dtime(9, 30)
    app._mikkel_off_since = None
    app._manual_hold = None
    app._pending_writes = collections.deque()

    clock = {"now": now}
    app._clock = clock
    app._now_local = lambda: clock["now"]

    app.states = states

    def call_service(service, **kw):
        # Mirror the write into the fake state store so latch/clear sequences see
        # their own effect, the same way HA would reflect it back.
        if service == "input_boolean/turn_on":
            states[kw["entity_id"]] = "on"
        elif service == "input_boolean/turn_off":
            states[kw["entity_id"]] = "off"

    app.call_service = MagicMock(side_effect=call_service)
    app.get_state = lambda entity, **kw: states.get(entity)
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    return app


def house_writes(app, kind):
    return [
        c for c in app.call_service.call_args_list
        if c[0][0] == f"input_boolean/turn_{kind}" and c[1]["entity_id"] == HOUSE
    ]


def run_minutely(app, start, end):
    """Drive the app the way production does: a tick every minute."""
    t = start
    while t <= end:
        app._clock["now"] = t
        app._tick({})
        t += timedelta(minutes=1)


class LatchOn(unittest.TestCase):
    def test_first_sleeper_inside_the_window_latches(self):
        states = {HOUSE: "off", MIKKEL: "off", KRISTINE: "on", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 22, 30))
        app._on_sleep_change(KRISTINE, "state", "off", "on", {})
        self.assertEqual(len(house_writes(app, "on")), 1)
        self.assertEqual(states[HOUSE], "on")

    def test_an_edge_outside_the_window_does_not_latch(self):
        # A 15:00 nap is not the house going to sleep.
        states = {HOUSE: "off", MIKKEL: "on", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 15, 0))
        app._on_sleep_change(MIKKEL, "state", "off", "on", {})
        self.assertEqual(house_writes(app, "on"), [])

    def test_already_asleep_at_window_start_latches_at_2000(self):
        # An early sleeper whose ON edge predates 20:00 still makes it night.
        states = {HOUSE: "off", MIKKEL: "on", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 20, 0))
        app._on_latch_window_start({})
        self.assertEqual(len(house_writes(app, "on")), 1)

    def test_no_latch_when_the_helper_does_not_exist_yet(self):
        states = {MIKKEL: "off", KRISTINE: "on", CLAUDIA: "off"}  # HOUSE missing
        app = make_app(states, now=datetime(2026, 8, 12, 22, 30))
        app._on_sleep_change(KRISTINE, "state", "off", "on", {})
        self.assertEqual(house_writes(app, "on"), [])

    def test_no_latch_when_already_on(self):
        states = {HOUSE: "on", MIKKEL: "off", KRISTINE: "on", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 22, 30))
        app._on_sleep_change(KRISTINE, "state", "off", "on", {})
        self.assertEqual(house_writes(app, "on"), [])


class IncidentNightOf20260807(unittest.TestCase):
    """2026-08-07, 04:14-04:44: out of bed 30 minutes mid-night. With the 12-min
    debounce his sleep boolean drops at 04:26; the sustained clear only counts
    minutes inside 04:30-12:00, so it would need him gone until 04:45 - he was back
    at 04:44. house_night_mode must survive the whole episode."""

    def test_house_night_mode_survives_the_0414_trip(self):
        states = {HOUSE: "on", MIKKEL: "on", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 7, 4, 26))

        # 04:26: the debounced sleep mode finally drops.
        states[MIKKEL] = "off"
        app._on_sleep_change(MIKKEL, "state", "on", "off", {})
        # Production reality: a tick every minute through the episode.
        run_minutely(app, datetime(2026, 8, 7, 4, 27), datetime(2026, 8, 7, 4, 43))
        # 04:44: back in bed, sleep mode re-arms.
        app._clock["now"] = datetime(2026, 8, 7, 4, 44)
        states[MIKKEL] = "on"
        app._on_sleep_change(MIKKEL, "state", "off", "on", {})
        run_minutely(app, datetime(2026, 8, 7, 4, 45), datetime(2026, 8, 7, 5, 30))

        self.assertEqual(house_writes(app, "off"), [])
        self.assertEqual(states[HOUSE], "on")

    def test_counter_control_staying_up_does_clear_at_0445(self):
        # Positive control: the same 04:26 drop with NO return to bed clears at
        # 04:45 sharp (04:30 + 15 sustained minutes), not earlier.
        states = {HOUSE: "on", MIKKEL: "on", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 7, 4, 26))
        states[MIKKEL] = "off"
        app._on_sleep_change(MIKKEL, "state", "on", "off", {})

        run_minutely(app, datetime(2026, 8, 7, 4, 27), datetime(2026, 8, 7, 4, 44))
        self.assertEqual(house_writes(app, "off"), [])  # 04:44: still night

        run_minutely(app, datetime(2026, 8, 7, 4, 45), datetime(2026, 8, 7, 4, 46))
        self.assertEqual(len(house_writes(app, "off")), 1)

    def test_minutes_off_before_0430_do_not_count(self):
        # Sleep mode off from 03:50 (a long trip): the clear still waits for 15
        # sustained minutes inside the window - 04:45, never 04:31.
        states = {HOUSE: "on", MIKKEL: "on", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 7, 3, 50))
        states[MIKKEL] = "off"
        app._on_sleep_change(MIKKEL, "state", "on", "off", {})

        run_minutely(app, datetime(2026, 8, 7, 3, 51), datetime(2026, 8, 7, 4, 44))
        self.assertEqual(house_writes(app, "off"), [])
        run_minutely(app, datetime(2026, 8, 7, 4, 45), datetime(2026, 8, 7, 4, 46))
        self.assertEqual(len(house_writes(app, "off")), 1)


class MorningClear(unittest.TestCase):
    def test_hard_clear_at_0930_even_while_someone_sleeps_in(self):
        states = {HOUSE: "on", MIKKEL: "on", KRISTINE: "on", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 9, 30))
        app._on_hard_clear({})
        self.assertEqual(len(house_writes(app, "off")), 1)

    def test_hard_clear_is_a_noop_when_already_off(self):
        states = {HOUSE: "off", MIKKEL: "off", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 9, 30))
        app._on_hard_clear({})
        self.assertEqual(house_writes(app, "off"), [])

    def test_unavailable_mikkel_boolean_is_not_evidence_he_is_up(self):
        states = {HOUSE: "on", MIKKEL: "unavailable", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 5, 0))
        run_minutely(app, datetime(2026, 8, 12, 5, 0), datetime(2026, 8, 12, 6, 0))
        self.assertEqual(house_writes(app, "off"), [])

    def test_restart_mid_morning_counts_from_now_not_from_zero(self):
        # _mikkel_off_since is None after a restart even though he is off: the
        # clear must wait a full sustain from the first tick, not fire instantly.
        states = {HOUSE: "on", MIKKEL: "off", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 6, 0))
        run_minutely(app, datetime(2026, 8, 12, 6, 0), datetime(2026, 8, 12, 6, 14))
        self.assertEqual(house_writes(app, "off"), [])
        run_minutely(app, datetime(2026, 8, 12, 6, 15), datetime(2026, 8, 12, 6, 16))
        self.assertEqual(len(house_writes(app, "off")), 1)


class ManualFlipsAreRespected(unittest.TestCase):
    """A human flipping the boolean wins until the next natural latch/clear
    boundary. Own writes are recognized via the expectation queue and never
    misread as manual."""

    def test_own_write_is_not_a_manual_hold(self):
        states = {HOUSE: "off", MIKKEL: "off", KRISTINE: "on", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 22, 30))
        app._on_sleep_change(KRISTINE, "state", "off", "on", {})  # our latch
        app._on_house_change(HOUSE, "state", "off", "on", {})     # HA reflects it
        self.assertIsNone(app._manual_hold)

    def test_manual_off_holds_for_the_rest_of_the_night(self):
        states = {HOUSE: "on", MIKKEL: "off", KRISTINE: "on", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 22, 10))
        states[HOUSE] = "off"
        app._on_house_change(HOUSE, "state", "on", "off", {})  # human turns it off
        self.assertEqual(app._manual_hold[0], "off")

        # Another sleeper's edge later that night must NOT re-latch.
        app._clock["now"] = datetime(2026, 8, 12, 23, 0)
        states[CLAUDIA] = "on"
        app._on_sleep_change(CLAUDIA, "state", "off", "on", {})
        self.assertEqual(house_writes(app, "on"), [])

    def test_manual_off_expires_at_the_next_latch_window_start(self):
        states = {HOUSE: "on", MIKKEL: "off", KRISTINE: "on", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 22, 10))
        states[HOUSE] = "off"
        app._on_house_change(HOUSE, "state", "on", "off", {})

        # Next evening 20:00: the daily latch-window-start check runs with Kristine
        # already asleep again - the hold has expired, night latches normally.
        app._clock["now"] = datetime(2026, 8, 13, 20, 0)
        app._on_latch_window_start({})
        self.assertEqual(len(house_writes(app, "on")), 1)

    def test_manual_on_gets_a_fresh_sustain_clock(self):
        # 05:00: Mikkel has been up since 04:00, the natural clear already fired at
        # 04:45. A human forces night back ON at 05:00 - it must survive at least
        # 15 minutes, then fall at the next natural sustained boundary (05:15).
        states = {HOUSE: "off", MIKKEL: "off", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 5, 0))
        app._mikkel_off_since = datetime(2026, 8, 12, 4, 0)
        states[HOUSE] = "on"
        app._on_house_change(HOUSE, "state", "off", "on", {})
        self.assertEqual(app._manual_hold[0], "on")

        run_minutely(app, datetime(2026, 8, 12, 5, 1), datetime(2026, 8, 12, 5, 14))
        self.assertEqual(house_writes(app, "off"), [])
        run_minutely(app, datetime(2026, 8, 12, 5, 15), datetime(2026, 8, 12, 5, 16))
        self.assertEqual(len(house_writes(app, "off")), 1)
        self.assertIsNone(app._manual_hold)

    def test_manual_on_in_the_afternoon_survives_until_the_hard_clear(self):
        states = {HOUSE: "off", MIKKEL: "off", KRISTINE: "off", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 13, 0))
        states[HOUSE] = "on"
        app._on_house_change(HOUSE, "state", "off", "on", {})

        # Ticks all afternoon (outside the clear window): nothing clears it.
        run_minutely(app, datetime(2026, 8, 12, 13, 1), datetime(2026, 8, 12, 13, 30))
        self.assertEqual(house_writes(app, "off"), [])

        # Next morning's hard clear is the natural boundary that ends the hold.
        app._clock["now"] = datetime(2026, 8, 13, 9, 30)
        app._on_hard_clear({})
        self.assertEqual(len(house_writes(app, "off")), 1)
        self.assertIsNone(app._manual_hold)

    def test_stale_pending_writes_do_not_swallow_a_manual_flip(self):
        # A service call that never produced a state change must not make a later
        # human flip look like our own write.
        states = {HOUSE: "off", MIKKEL: "off", KRISTINE: "on", CLAUDIA: "off"}
        app = make_app(states, now=datetime(2026, 8, 12, 22, 0))
        app._pending_writes.append(("on", datetime(2026, 8, 12, 21, 0)))  # 1h stale
        states[HOUSE] = "on"
        app._on_house_change(HOUSE, "state", "off", "on", {})
        self.assertIsNotNone(app._manual_hold)


class HelperCreation(unittest.TestCase):
    def _app(self, states):
        app = make_app(states, now=datetime(2026, 8, 12, 12, 0))
        app.helper_name = "House night mode"
        app.helper_retry_delays = [30, 60]
        app._created_helper = False
        app.run_in = MagicMock()
        app._create_helper_ws = MagicMock(return_value={"id": "house_night_mode"})
        return app

    def test_missing_helper_is_created(self):
        app = self._app({MIKKEL: "off"})
        app._ensure_helper({})
        app._create_helper_ws.assert_called_once_with("House night mode", "mdi:weather-night")
        app.run_in.assert_called_once()

    def test_existing_helper_is_left_alone(self):
        app = self._app({HOUSE: "off", MIKKEL: "off"})
        app._ensure_helper({})
        app._create_helper_ws.assert_not_called()

    def test_never_creates_twice(self):
        app = self._app({MIKKEL: "off"})
        app._ensure_helper({})
        app._ensure_helper({"attempt": 1})  # AD's view still lagging
        app._create_helper_ws.assert_called_once()


if __name__ == "__main__":
    unittest.main()
