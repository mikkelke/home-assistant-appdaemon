# tests/test_housemate_sleep_mode.py - HousemateSleepMode: server-side sleep booleans
# for Kristine and Claudia from person.* + companion battery_state + room PIR only.
# Encodes the 2026-08-07 failure that motivated it (Kristine's phone-side helper
# pipeline dropped one edge and her sleep mode read OFF all night while the phone
# charged) as behaviour: the battery SENSOR is the source, dropouts hold state, and
# her flappy tracker gets a leave grace. Same __new__ + monkeypatched-callables
# harness as test_actor_attribution.py.
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

import housemate_sleep_mode as hsm  # noqa: E402


NIGHT = datetime(2026, 8, 12, 22, 30)  # inside the 21:00-10:00 window
DAY = datetime(2026, 8, 12, 15, 0)


def make_person(key="kristine", **overrides):
    p = {
        "key": key,
        "person_entity": f"person.{key}",
        "battery_entity": f"sensor.{key}_battery_state",
        "pir_entity": f"binary_sensor.{key}s_room_pir_presence",
        "sleep_entity": f"input_boolean.{key}_sleep_mode",
        "helper_name": f"{key} sleep mode",
        "leave_grace_seconds": 10 * 60,
        "verdict": False,
        "person_state": "home",
        "battery_state": "charging",
        "pir_state": "off",
        "last_motion": NIGHT - timedelta(minutes=30),
        "battery_off_since": None,
        "away_since": None,
        "rearm_block": False,
        "expected_writes": collections.deque(),
    }
    p.update(overrides)
    return p


def make_app(*people, now=NIGHT):
    app = hsm.HousemateSleepMode.__new__(hsm.HousemateSleepMode)
    app.night_window = (dtime(21, 0), dtime(10, 0))
    app.quiet_seconds = 10 * 60
    app.unplug_seconds = 5 * 60
    app.on_battery_states = frozenset({"charging", "full"})
    app._people = {p["key"]: p for p in people}

    clock = {"now": now}
    app._clock = clock
    app._now_local = lambda: clock["now"]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.call_service = MagicMock()
    return app


def turn_ons(app):
    return [c for c in app.call_service.call_args_list if c[0][0] == "input_boolean/turn_on"]


def turn_offs(app):
    return [c for c in app.call_service.call_args_list if c[0][0] == "input_boolean/turn_off"]


class TurnsOnWhenSettled(unittest.TestCase):
    """The girls' ON: home + battery Charging/Full + night window + room PIR quiet
    for >= 10 min."""

    def test_home_charging_night_and_quiet_turns_on(self):
        p = make_person()
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(len(turn_ons(app)), 1)
        self.assertTrue(p["verdict"])

    def test_full_battery_counts_as_plugged_in(self):
        # Real night 2026-08-11: Claudia's phone hit "Full" at 06:37 local while
        # still on the charger - Full must never read as unplugged.
        p = make_person(battery_state="full")
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(len(turn_ons(app)), 1)

    def test_recent_motion_delays_on_until_the_room_settles(self):
        p = make_person(last_motion=NIGHT - timedelta(minutes=3))
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(turn_ons(app), [])
        # ...they settled: the same conditions 8 minutes later satisfy the quiet gate.
        app._clock["now"] = NIGHT + timedelta(minutes=8)
        app._evaluate(p)
        self.assertEqual(len(turn_ons(app)), 1)

    def test_pir_currently_on_blocks_on(self):
        p = make_person(pir_state="on", last_motion=NIGHT - timedelta(minutes=30))
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(turn_ons(app), [])

    def test_not_charging_blocks_on(self):
        p = make_person(battery_state="not charging")
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(turn_ons(app), [])

    def test_away_blocks_on(self):
        p = make_person(person_state="not_home")
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(turn_ons(app), [])

    def test_daytime_blocks_on(self):
        p = make_person(last_motion=DAY - timedelta(minutes=30))
        app = make_app(p, now=DAY)
        app._evaluate(p)
        self.assertEqual(turn_ons(app), [])


class ConservativeOff(unittest.TestCase):
    """OFF is only: unplugged >= 5 min, away >= leave-grace, or the hard morning
    end. Dropouts and PIR activity hold state."""

    def test_unplug_sustained_5_minutes_turns_off(self):
        p = make_person(verdict=True, battery_state="not charging",
                        battery_off_since=NIGHT - timedelta(minutes=5))
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(len(turn_offs(app)), 1)
        self.assertFalse(p["verdict"])

    def test_unplug_shorter_than_5_minutes_holds(self):
        p = make_person(verdict=True, battery_state="not charging",
                        battery_off_since=NIGHT - timedelta(minutes=3))
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(turn_offs(app), [])

    def test_replug_cancels_the_unplug_clock(self):
        p = make_person(verdict=True, battery_state="not charging",
                        battery_off_since=NIGHT - timedelta(minutes=3))
        app = make_app(p)
        app._on_battery(p["battery_entity"], "state", "Not Charging", "Charging",
                        {"person": p["key"]})
        self.assertIsNone(p["battery_off_since"])
        app._clock["now"] = NIGHT + timedelta(minutes=30)
        app._evaluate(p)
        self.assertEqual(turn_offs(app), [])

    def test_battery_dropout_is_not_an_unplug(self):
        # unknown/unavailable is what the companion sensor reads when the phone
        # sleeps hard - holding is the whole point.
        p = make_person(verdict=True)
        app = make_app(p)
        app._on_battery(p["battery_entity"], "state", "Charging", "unavailable",
                        {"person": p["key"]})
        self.assertIsNone(p["battery_off_since"])
        app._clock["now"] = NIGHT + timedelta(hours=2)
        app._evaluate(p)
        self.assertEqual(turn_offs(app), [])

    def test_tracker_blip_within_grace_does_not_clear(self):
        # Regression guard: person.kristine flaps at the home-zone boundary. A
        # home -> not_home -> home blip inside the grace must not end her night.
        p = make_person(verdict=True)
        app = make_app(p)
        app._on_person(p["person_entity"], "state", "home", "not_home", {"person": p["key"]})
        self.assertIsNotNone(p["away_since"])
        app._clock["now"] = NIGHT + timedelta(minutes=3)
        app._on_person(p["person_entity"], "state", "not_home", "home", {"person": p["key"]})
        self.assertIsNone(p["away_since"])
        app._clock["now"] = NIGHT + timedelta(minutes=30)
        app._evaluate(p)
        self.assertEqual(turn_offs(app), [])

    def test_leaving_home_sustained_clears_after_grace(self):
        p = make_person(verdict=True, person_state="not_home",
                        away_since=NIGHT - timedelta(minutes=10))
        app = make_app(p)
        app._evaluate(p)
        self.assertEqual(len(turn_offs(app)), 1)

    def test_tracker_dropout_mid_grace_does_not_reset_the_clock(self):
        p = make_person(verdict=True, person_state="not_home",
                        away_since=NIGHT - timedelta(minutes=8))
        app = make_app(p)
        app._on_person(p["person_entity"], "state", "not_home", "unavailable",
                       {"person": p["key"]})
        self.assertIsNotNone(p["away_since"])
        app._clock["now"] = NIGHT + timedelta(minutes=2)
        app._evaluate(p)
        self.assertEqual(len(turn_offs(app)), 1)

    def test_hard_morning_end_at_window_close(self):
        p = make_person(verdict=True)
        app = make_app(p, now=datetime(2026, 8, 13, 10, 0))
        app._evaluate(p)
        self.assertEqual(len(turn_offs(app)), 1)

    def test_pir_activity_never_clears(self):
        # 2026-08-12 01:23 local: Claudia's PIR fired for 2 min mid-sleep. Motion
        # is not wake-up evidence at night.
        p = make_person(verdict=True)
        app = make_app(p)
        app._on_pir(p["pir_entity"], "state", "off", "on", {"person": p["key"]})
        app._clock["now"] = NIGHT + timedelta(minutes=20)
        app._evaluate(p)
        self.assertEqual(turn_offs(app), [])


class KristineNightOf20260807(unittest.TestCase):
    """The regression that motivated the app, end to end on her real timeline:
    home all evening, phone Charging from 23:30 local, room settles - her sleep
    boolean must come on and stay on until the 06:50 unplug + 5 min."""

    def test_full_night(self):
        plug_in = datetime(2026, 8, 6, 23, 30)
        # She is moving right up to plugging the phone in - the quiet clock starts there.
        p = make_person(battery_state="not charging", last_motion=plug_in)
        app = make_app(p, now=plug_in)

        app._on_battery(p["battery_entity"], "state", "Not Charging", "Charging",
                        {"person": p["key"]})
        self.assertEqual(turn_ons(app), [])  # room not yet quiet for 10 min

        app._clock["now"] = plug_in + timedelta(minutes=9)
        app._evaluate(p)
        self.assertEqual(turn_ons(app), [])

        app._clock["now"] = plug_in + timedelta(minutes=11)
        app._evaluate(p)
        self.assertEqual(len(turn_ons(app)), 1)  # 23:41 - on for the night

        # Ticks through the night hold it on.
        for hour_offset in range(1, 7):
            app._clock["now"] = plug_in + timedelta(hours=hour_offset)
            app._evaluate(p)
        self.assertEqual(turn_offs(app), [])

        # 06:50: unplug. Clear follows 5 min later, not instantly.
        unplug = datetime(2026, 8, 7, 6, 50)
        app._clock["now"] = unplug
        app._on_battery(p["battery_entity"], "state", "Charging", "Not Charging",
                        {"person": p["key"]})
        self.assertEqual(turn_offs(app), [])
        app._clock["now"] = unplug + timedelta(minutes=5)
        app._evaluate(p)
        self.assertEqual(len(turn_offs(app)), 1)


class ForeignWritesAdoptedNotFought(unittest.TestCase):
    """Until automation.kristine_sleep_mode_control is disabled at deploy time it
    keeps writing her boolean; humans can too. Last-writer-wins: adopt, never
    revert - and a foreign OFF must not be flipped back on a minute later."""

    def test_own_write_is_not_treated_as_foreign(self):
        p = make_person()
        app = make_app(p)
        app._evaluate(p)  # our ON
        app._on_sleep_boolean(p["sleep_entity"], "state", "off", "on", {"person": p["key"]})
        self.assertTrue(p["verdict"])
        self.assertFalse(p["rearm_block"])

    def test_foreign_off_blocks_rearm_while_conditions_still_hold(self):
        p = make_person(verdict=True)
        app = make_app(p)
        app._on_sleep_boolean(p["sleep_entity"], "state", "on", "off", {"person": p["key"]})
        self.assertFalse(p["verdict"])
        self.assertTrue(p["rearm_block"])
        app._clock["now"] = NIGHT + timedelta(minutes=30)
        app._evaluate(p)  # home + charging + quiet + night: would turn on, must not
        self.assertEqual(turn_ons(app), [])

    def test_rearm_block_releases_when_an_on_condition_breaks(self):
        p = make_person(verdict=True)
        app = make_app(p)
        app._on_sleep_boolean(p["sleep_entity"], "state", "on", "off", {"person": p["key"]})
        # Next morning she unplugs - the block releases at that natural boundary...
        app._clock["now"] = NIGHT + timedelta(hours=8)
        app._on_battery(p["battery_entity"], "state", "Charging", "Not Charging",
                        {"person": p["key"]})
        self.assertFalse(p["rearm_block"])
        # ...and the following evening arms normally.
        p["battery_state"] = "charging"
        p["last_motion"] = NIGHT + timedelta(days=1) - timedelta(minutes=30)
        app._clock["now"] = NIGHT + timedelta(days=1)
        app._evaluate(p)
        self.assertEqual(len(turn_ons(app)), 1)

    def test_foreign_on_is_adopted_and_our_off_rules_take_over(self):
        p = make_person(battery_state="not charging")
        app = make_app(p)
        app._on_sleep_boolean(p["sleep_entity"], "state", "off", "on", {"person": p["key"]})
        self.assertTrue(p["verdict"])
        self.assertIsNotNone(p["battery_off_since"])  # already-broken condition clocked
        app._clock["now"] = NIGHT + timedelta(minutes=5)
        app._evaluate(p)
        self.assertEqual(len(turn_offs(app)), 1)


class HelperCreation(unittest.TestCase):
    """Create-if-missing for the sleep booleans: checks existence first, never
    creates the same name twice (a repeat create would mint claudia_sleep_mode_2),
    verifies via re-check, retries with backoff while HA is still coming up."""

    def _app_with(self, states, people):
        app = make_app(*people)
        app.helper_retry_delays = [30, 60]
        app._created_helper_names = set()
        app.get_state = lambda entity, **kw: states.get(entity)
        app.run_in = MagicMock()
        app._create_helper_ws = MagicMock(
            side_effect=lambda name, icon: {"id": name.lower().replace(" ", "_")}
        )
        return app

    def test_missing_helper_is_created_and_verification_scheduled(self):
        claudia = make_person("claudia", helper_name="Claudia sleep mode")
        states = {}  # claudia's boolean does not exist
        app = self._app_with(states, [claudia])
        app._ensure_helpers({})
        app._create_helper_ws.assert_called_once_with("Claudia sleep mode", "mdi:sleep")
        app.run_in.assert_called_once()  # re-verify pass scheduled

    def test_existing_helper_is_left_alone(self):
        kristine = make_person("kristine", helper_name="Kristine sleep mode")
        states = {kristine["sleep_entity"]: "off"}
        app = self._app_with(states, [kristine])
        app._ensure_helpers({})
        app._create_helper_ws.assert_not_called()
        app.run_in.assert_not_called()

    def test_a_name_is_never_created_twice(self):
        claudia = make_person("claudia", helper_name="Claudia sleep mode")
        app = self._app_with({}, [claudia])
        app._ensure_helpers({})
        # AD's view still lags (entity not visible yet) - the retry must only
        # verify, not create a duplicate.
        app._ensure_helpers({"attempt": 1})
        app._create_helper_ws.assert_called_once()

    def test_retries_stop_at_the_backoff_ceiling(self):
        claudia = make_person("claudia", helper_name="Claudia sleep mode")
        app = self._app_with({}, [claudia])
        app._ensure_helpers({"attempt": 2})  # past the 2 configured delays
        app.run_in.assert_not_called()
        self.assertTrue(any("gave up" in str(a) for a, kw in app.log_calls))

    def test_unexpected_created_id_is_reported_not_deleted(self):
        claudia = make_person("claudia", helper_name="Claudias sleep mode")  # wrong name
        app = self._app_with({}, [claudia])
        app._ensure_helpers({})
        self.assertTrue(any("expected" in str(a) for a, kw in app.log_calls))


class StartupSeeding(unittest.TestCase):
    """The boolean's current state IS the verdict at init, and clocks re-seed from
    HA's last_changed - a restart mid-night must not clear or re-arm anything."""

    def _init_app(self, states):
        app = hsm.HousemateSleepMode.__new__(hsm.HousemateSleepMode)
        app.args = {
            "people": [{
                "name": "kristine",
                "person_entity": "person.kristine",
                "battery_entity": "sensor.kristine_battery_state",
                "pir_entity": "binary_sensor.kristines_room_pir_presence",
                "sleep_entity": "input_boolean.kristine_sleep_mode",
                "helper_name": "Kristine sleep mode",
            }],
        }
        app.get_state = lambda entity, **kw: states.get(
            (entity, kw.get("attribute")) if kw.get("attribute") else entity
        )
        app.listen_state = MagicMock()
        app.run_in = MagicMock()
        app.run_every = MagicMock()
        app.log = MagicMock()
        app.call_service = MagicMock()
        app.initialize()
        return app

    def test_boolean_on_at_startup_is_adopted_as_verdict(self):
        app = self._init_app({
            "input_boolean.kristine_sleep_mode": "on",
            "person.kristine": "home",
            "sensor.kristine_battery_state": "Charging",
            "binary_sensor.kristines_room_pir_presence": "off",
        })
        self.assertTrue(app._people["kristine"]["verdict"])
        self.assertIsNone(app._people["kristine"]["battery_off_since"])

    def test_running_unplug_clock_reseeds_from_last_changed(self):
        app = self._init_app({
            "input_boolean.kristine_sleep_mode": "on",
            "person.kristine": "home",
            "sensor.kristine_battery_state": "Not Charging",
            ("sensor.kristine_battery_state", "last_changed"): "2026-08-12T04:38:29+00:00",
            "binary_sensor.kristines_room_pir_presence": "off",
        })
        self.assertIsNotNone(app._people["kristine"]["battery_off_since"])

    def test_missing_boolean_seeds_verdict_off(self):
        app = self._init_app({
            "person.kristine": "home",
            "sensor.kristine_battery_state": "Charging",
            "binary_sensor.kristines_room_pir_presence": "off",
        })
        self.assertFalse(app._people["kristine"]["verdict"])


if __name__ == "__main__":
    unittest.main()
