# tests/test_mikkel_sleep_mode_night.py - the 2026-08-12 night-regime changes to
# mikkel_sleep_mode.py: (i) ON is gated by the bedroom being in sleep configuration
# (bottom-up blind current_position >= 95 = covering, OR inside 21:00-04:00), so a
# daytime in-bed charge with the blind parked open no longer flips sleep + DND;
# (ii) while input_boolean.house_night_mode is on, out-of-bed only clears after
# >= 12 min sustained (2026-08-07 04:14-04:44: a 30-min mid-night trip out of bed
# used to clear instantly and cascade into the whole house leaving night mode) and
# discharging after >= 5 min sustained, while daytime keeps the immediate clears.
# Same __new__ + monkeypatched-callables harness as test_mikkel_sleep_mode.py.
# Run from repo root: python3 -m unittest discover -s apps/rutines/tests -q

from __future__ import annotations

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

import mikkel_sleep_mode as msm  # noqa: E402


def make_app(states, *, now, blind_position=None, block_rearm=False):
    """MikkelSleepMode with the night gate and house-night debounce configured the
    way the deployed yaml configures them, a controllable clock, and a get_state
    that understands the blind's attribute read."""
    app = msm.MikkelSleepMode.__new__(msm.MikkelSleepMode)
    app.battery_entity = "sensor.mikkels_ofx9p_battery_state"
    app.person_entity = "person.mikkel"
    app.in_bed_entities = ["binary_sensor.left_bedside", "binary_sensor.right_bedside"]
    app.sleep_mode_entity = "input_boolean.mikkel_sleep_mode"
    app._on_battery_states = frozenset(["charging", "not_charging"])
    app._off_battery_state = "discharging"
    app._dnd_on_command = "priority_only"
    app._dnd_off_command = "off"
    app._notify_service_path = "notify/mobile_app_test"

    app.blind_entity = "cover.bedroom_blind"
    app.blind_covering_min = 95.0
    app.night_window = (dtime(21, 0), dtime(4, 0))
    app.house_night_entity = "input_boolean.house_night_mode"
    app.out_of_bed_clear_seconds = 12 * 60
    app.discharging_clear_seconds = 5 * 60
    app.discharging_up_clear_seconds = 20
    app._out_of_bed_since = None
    app._discharging_since = None

    clock = {"now": now}
    app._clock = clock
    app._now_local = lambda: clock["now"]

    blind = {"position": blind_position}
    app._blind = blind

    def get_state(entity, **kw):
        if entity == "cover.bedroom_blind" and kw.get("attribute") == "current_position":
            return blind["position"]
        return states.get(entity)

    app.get_state = get_state
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.call_service = MagicMock()

    app._block_rearm_until_out_of_bed = block_rearm
    app._state = {"block_rearm_until_out_of_bed": block_rearm}
    app._save_state = MagicMock()
    return app


def turn_ons(app):
    return [c for c in app.call_service.call_args_list if c[0][0] == "input_boolean/turn_on"]


def turn_offs(app):
    return [c for c in app.call_service.call_args_list if c[0][0] == "input_boolean/turn_off"]


IN_BED_CHARGING = {
    "sensor.mikkels_ofx9p_battery_state": "charging",
    "person.mikkel": "home",
    "binary_sensor.left_bedside": "on",
    "binary_sensor.right_bedside": "off",
    "input_boolean.mikkel_sleep_mode": "off",
    "input_boolean.house_night_mode": "off",
}

DAY = datetime(2026, 8, 12, 13, 0)
EVENING = datetime(2026, 8, 12, 22, 30)


class NightGateArmsOn(unittest.TestCase):
    """Regression 2026-08-12: in bed + charger + blind parked open (position 38) in
    the afternoon used to flip sleep mode + phone DND. The blind is BOTTOM-UP:
    position 100 = risen = covering the window = sleep configuration."""

    def test_daytime_bed_and_charger_with_blind_parked_open_stays_off(self):
        app = make_app(dict(IN_BED_CHARGING), now=DAY, blind_position=38)
        app._apply_sleep_mode()
        self.assertEqual(turn_ons(app), [])

    def test_evening_bed_and_charger_turns_on_even_with_blind_parked_open(self):
        # 22:30 is inside 21:00-04:00, so the time window alone opens the gate.
        app = make_app(dict(IN_BED_CHARGING), now=EVENING, blind_position=38)
        app._apply_sleep_mode()
        self.assertEqual(len(turn_ons(app)), 1)

    def test_evening_bed_and_charger_with_blind_covering_turns_on(self):
        app = make_app(dict(IN_BED_CHARGING), now=EVENING, blind_position=100)
        app._apply_sleep_mode()
        self.assertEqual(len(turn_ons(app)), 1)

    def test_daytime_nap_with_blind_covering_turns_on(self):
        # Deliberate: covering the window IS the sleep configuration, whatever the
        # clock says - his complaint was only about the blind being parked open.
        app = make_app(dict(IN_BED_CHARGING), now=DAY, blind_position=100)
        app._apply_sleep_mode()
        self.assertEqual(len(turn_ons(app)), 1)

    def test_unreadable_blind_position_daytime_stays_off(self):
        # Only the time window can open the gate when the position can't be read.
        app = make_app(dict(IN_BED_CHARGING), now=DAY, blind_position=None)
        app._apply_sleep_mode()
        self.assertEqual(turn_ons(app), [])

    def test_dnd_still_pushed_when_sleep_arms(self):
        app = make_app(dict(IN_BED_CHARGING), now=EVENING, blind_position=100)
        app._apply_sleep_mode()
        dnd = [
            c for c in app.call_service.call_args_list
            if c[0][0] == "notify/mobile_app_test"
            and c[1].get("data", {}).get("command") == "priority_only"
        ]
        self.assertEqual(len(dnd), 1)

    def test_gate_never_clears_an_already_on_sleep_mode(self):
        # 04:10: window over, blind parked open (say a summer night with the blind
        # down), still in bed + charging - sleep mode must stay on.
        states = dict(IN_BED_CHARGING)
        states["input_boolean.mikkel_sleep_mode"] = "on"
        app = make_app(states, now=datetime(2026, 8, 13, 4, 10), blind_position=38)
        app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])

    def test_bare_instances_keep_the_legacy_open_gate(self):
        # The older tests build instances via __new__ with no gate config at all -
        # class defaults must leave the gate open for them.
        app = msm.MikkelSleepMode.__new__(msm.MikkelSleepMode)
        self.assertTrue(app._night_gate_open())


class NightModeOffDebounce(unittest.TestCase):
    """Regression 2026-08-07 04:14-04:44: out of bed for 30 min mid-night. With the
    house in night mode, sleep mode must hold for the first 12 min out of bed and
    clear only after that; a return to bed within 12 min cancels the clear."""

    def _night_app(self, t0):
        states = dict(IN_BED_CHARGING)
        states["input_boolean.mikkel_sleep_mode"] = "on"
        states["input_boolean.house_night_mode"] = "on"
        app = make_app(states, now=t0, blind_position=100)
        return app, states

    def test_out_of_bed_holds_for_the_first_12_minutes(self):
        t0 = datetime(2026, 8, 7, 4, 14)
        app, states = self._night_app(t0)
        states["binary_sensor.left_bedside"] = "off"  # out of bed at 04:14

        for minutes in (0, 5, 11):
            app._clock["now"] = t0 + timedelta(minutes=minutes)
            app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])

    def test_out_of_bed_clears_after_12_minutes(self):
        t0 = datetime(2026, 8, 7, 4, 14)
        app, states = self._night_app(t0)
        states["binary_sensor.left_bedside"] = "off"

        app._apply_sleep_mode()  # starts the clock at 04:14
        app._clock["now"] = t0 + timedelta(minutes=12)
        app._apply_sleep_mode()  # 04:26
        self.assertEqual(len(turn_offs(app)), 1)

    def test_return_to_bed_within_12_minutes_cancels_the_clear(self):
        t0 = datetime(2026, 8, 7, 4, 14)
        app, states = self._night_app(t0)
        states["binary_sensor.left_bedside"] = "off"

        app._apply_sleep_mode()
        app._clock["now"] = t0 + timedelta(minutes=8)
        states["binary_sensor.left_bedside"] = "on"  # back in bed
        app._apply_sleep_mode()
        app._clock["now"] = t0 + timedelta(minutes=20)
        app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])
        self.assertIsNone(app._out_of_bed_since)

    def test_discharging_clears_after_5_minutes_not_immediately(self):
        t0 = datetime(2026, 8, 7, 3, 0)
        app, states = self._night_app(t0)
        states["sensor.mikkels_ofx9p_battery_state"] = "discharging"

        app._apply_sleep_mode()
        app._clock["now"] = t0 + timedelta(minutes=3)
        app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])
        app._clock["now"] = t0 + timedelta(minutes=5)
        app._apply_sleep_mode()
        self.assertEqual(len(turn_offs(app)), 1)

    def test_charging_again_within_5_minutes_cancels_the_discharge_clear(self):
        t0 = datetime(2026, 8, 7, 3, 0)
        app, states = self._night_app(t0)
        states["sensor.mikkels_ofx9p_battery_state"] = "discharging"

        app._apply_sleep_mode()
        app._clock["now"] = t0 + timedelta(minutes=3)
        states["sensor.mikkels_ofx9p_battery_state"] = "charging"
        app._apply_sleep_mode()
        app._clock["now"] = t0 + timedelta(minutes=10)
        app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])

    def test_battery_unavailable_at_night_holds_sleep_mode(self):
        # The companion sensor goes stale exactly when the phone sleeps - that is
        # not evidence he got up, and at night it must not clear anything.
        t0 = datetime(2026, 8, 7, 3, 0)
        app, states = self._night_app(t0)
        states["sensor.mikkels_ofx9p_battery_state"] = "unavailable"

        for minutes in (0, 10, 60):
            app._clock["now"] = t0 + timedelta(minutes=minutes)
            app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])

    def test_explicitly_leaving_home_at_night_clears_immediately(self):
        t0 = datetime(2026, 8, 7, 2, 0)
        app, states = self._night_app(t0)
        states["person.mikkel"] = "not_home"
        app._apply_sleep_mode()
        self.assertEqual(len(turn_offs(app)), 1)

    def test_daytime_keeps_the_immediate_clear(self):
        # House night mode off: today's behaviour, unchanged - out of bed clears at
        # once, no 12-min grace.
        states = dict(IN_BED_CHARGING)
        states["input_boolean.mikkel_sleep_mode"] = "on"
        states["binary_sensor.left_bedside"] = "off"
        app = make_app(states, now=DAY, blind_position=100)
        app._apply_sleep_mode()
        self.assertEqual(len(turn_offs(app)), 1)

    def test_clocks_reset_once_sleep_mode_is_off(self):
        t0 = datetime(2026, 8, 7, 4, 14)
        app, states = self._night_app(t0)
        states["binary_sensor.left_bedside"] = "off"
        app._apply_sleep_mode()
        self.assertIsNotNone(app._out_of_bed_since)

        states["input_boolean.mikkel_sleep_mode"] = "off"
        app._apply_sleep_mode()
        self.assertIsNone(app._out_of_bed_since)
        self.assertIsNone(app._discharging_since)


if __name__ == "__main__":
    unittest.main()


class UnplugWhileUpClearsFast(unittest.TestCase):
    """2026-08-13: unplugging while already OUT of bed is decisively "up for the day", yet
    the 5-min anti-cable-pop debounce still applied - so Mikkel killed DND by thumb 18 s
    after unplugging (06:48:43) while the app's command trailed uselessly at 06:53:36. The
    two signals cross-confirm, so a token 20 s debounce is enough. The 5-min rule stays for
    the one case it protects: a cable popping out while he is STILL asleep in bed."""

    def _up_and_unplugged(self, t0):
        states = dict(IN_BED_CHARGING)
        states["input_boolean.mikkel_sleep_mode"] = "on"
        states["input_boolean.house_night_mode"] = "on"
        states["binary_sensor.left_bedside"] = "off"
        states["sensor.mikkels_ofx9p_battery_state"] = "discharging"
        app = make_app(states, now=t0, blind_position=100)
        return app, states

    def test_unplugged_and_out_of_bed_clears_after_token_debounce(self):
        t0 = datetime(2026, 8, 13, 6, 48, 25)
        app, states = self._up_and_unplugged(t0)
        app._apply_sleep_mode()  # starts both clocks
        app._clock["now"] = t0 + timedelta(seconds=25)
        app._apply_sleep_mode()
        self.assertEqual(len(turn_offs(app)), 1)

    def test_holds_within_the_token_debounce(self):
        t0 = datetime(2026, 8, 13, 6, 48, 25)
        app, states = self._up_and_unplugged(t0)
        app._apply_sleep_mode()
        app._clock["now"] = t0 + timedelta(seconds=10)
        app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])

    def test_cable_pop_while_asleep_keeps_the_five_minute_rule(self):
        t0 = datetime(2026, 8, 13, 3, 0)
        states = dict(IN_BED_CHARGING)
        states["input_boolean.mikkel_sleep_mode"] = "on"
        states["input_boolean.house_night_mode"] = "on"
        states["sensor.mikkels_ofx9p_battery_state"] = "discharging"  # still IN bed
        app = make_app(states, now=t0, blind_position=100)
        app._apply_sleep_mode()
        app._clock["now"] = t0 + timedelta(minutes=4)
        app._apply_sleep_mode()
        self.assertEqual(turn_offs(app), [])
        app._clock["now"] = t0 + timedelta(minutes=5, seconds=5)
        app._apply_sleep_mode()
        self.assertEqual(len(turn_offs(app)), 1)
