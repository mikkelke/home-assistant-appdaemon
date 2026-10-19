# tests/test_heating_signals.py - the floor-heating controller's signal rules (heating_signals.py):
# who an account is, the service-call buffer, who made a thermostat change, what each change
# means and how a nudge steps. Pure logic, so no AppDaemon harness is needed.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import heating_plan as hp  # noqa: E402
import heating_signals as sig  # noqa: E402

T0 = 1_800_000_000.0
APP = "f4fda494358943beaf8ad2c70db099f8"
MIKKEL = "11111111111111111111111111111111"
KRISTINE = "5bee78148825439488b70f92565e6c26"
CLAUDIA = "33333333333333333333333333333333"
STRANGER = "44444444444444444444444444444444"
THERMOSTATS = {
    "family": "climate.family_room_thermostat",
    "kristines_room": "climate.kristines_room_thermostat",
    "claudias_room": "climate.claudias_room_thermostat",
}


def zone(name, **extra):
    raw = {
        "thermostat": THERMOSTATS[name],
        "control_air": ["sensor.x"],
        "target_entity": f"input_number.heating_target_{name}",
        "base_c": 21.5,
        "lower_limit_c": 19.0,
        "upper_limit_c": 24.0,
    }
    raw.update(extra)
    return raw


CFG = hp.parse_config(
    {
        "season_entity": "input_boolean.heating_season",
        "zones": {
            "family": zone("family"),
            "kristines_room": zone(
                "kristines_room", residents=["kristine"], nudge={"min": -1.0, "max": 1.0}
            ),
            "claudias_room": zone(
                "claudias_room", residents=["claudia"], nudge={"min": -1.0, "max": 1.0}
            ),
        },
    }
)
FAMILY, KRISTINES, CLAUDIAS = (CFG.zones[n] for n in ("family", "kristines_room", "claudias_room"))
IDENTITY = sig.Identity("mikkel", APP, {MIKKEL: "mikkel", KRISTINE: "kristine", CLAUDIA: "claudia"})


def call(
    ts,
    service="set_temperature",
    entity=THERMOSTATS["kristines_room"],
    user=None,
    parent=None,
    **data,
):
    return sig.CallRecord(ts, entity, service, dict(data, entity_id=entity), user, parent)


def buffer(*calls):
    b = sig.CallBuffer()
    for c in calls:
        b.add(c)
    return b


def setpoint(new, ts=T0 + 5, old=21.5):
    return sig.Change(sig.KIND_SETPOINT, old, new, ts)


class Accounts(unittest.TestCase):
    def test_an_account_is_a_person_the_app_an_automation_or_a_stranger(self):
        self.assertEqual(IDENTITY.actor(MIKKEL, None), sig.Actor("person", "mikkel"))
        self.assertEqual(IDENTITY.actor(KRISTINE, "x"), sig.Actor("person", "kristine"))
        self.assertEqual(IDENTITY.actor(APP, None), sig.Actor("app", "appdaemon"))
        self.assertEqual(IDENTITY.actor(None, "ctx"), sig.Actor("automation"))
        self.assertEqual(IDENTITY.actor(STRANGER, None), sig.Actor("unknown_user"))
        self.assertEqual(IDENTITY.actor(None, None), sig.Actor("none"))
        self.assertEqual(IDENTITY.actor("", ""), sig.Actor("none"))

    def test_account_ids_compare_undashed_and_lowercase(self):
        dashed = "5BEE7814-8825-4394-88B7-0F92565E6C26"
        self.assertEqual(sig.normalize_user_id(dashed), KRISTINE)
        self.assertEqual(IDENTITY.actor(dashed, None), sig.Actor("person", "kristine"))
        loose = sig.Identity("mikkel", APP.upper(), {})
        self.assertEqual(loose.actor(APP, None).kind, "app")

    def test_without_an_app_account_nothing_reads_as_the_app(self):
        self.assertEqual(sig.Identity("mikkel").actor(APP, None).kind, "unknown_user")

    def test_with_users_replaces_the_map_only(self):
        again = IDENTITY.with_users({STRANGER: "guest"})
        self.assertEqual(again.actor(STRANGER, None), sig.Actor("person", "guest"))
        self.assertEqual(again.actor(APP, None).kind, "app")
        self.assertEqual(IDENTITY.actor(STRANGER, None).kind, "unknown_user")

    def test_how_an_actor_is_named_in_the_log(self):
        self.assertEqual(sig.describe(sig.Actor("person", "mikkel")), (sig.SRC_HA, "mikkel"))
        self.assertEqual(sig.describe(sig.Actor("app", "appdaemon")), (sig.SRC_APP, "appdaemon"))
        self.assertEqual(sig.describe(sig.Actor("automation")), (sig.SRC_AUTOMATION, None))
        self.assertEqual(sig.describe(sig.Actor("unknown_user")), (sig.SRC_UNKNOWN, None))
        self.assertEqual(sig.describe(sig.Actor("none")), (sig.SRC_UNKNOWN, None))
        self.assertEqual(sig.who_label(sig.SRC_HA, "mikkel"), "mikkel")
        self.assertEqual(sig.who_label(sig.SRC_AUTOMATION, None), "automation")
        self.assertEqual(sig.who_label(sig.SRC_KEYPAD, None), "unknown")


class CallRecords(unittest.TestCase):
    def event(self, entity_id, **extra):
        return {
            "domain": "climate",
            "service": "set_temperature",
            "service_data": dict(extra, entity_id=entity_id),
            "metadata": {"context": {"id": "c", "user_id": MIKKEL, "parent_id": None}},
        }

    def test_one_record_per_watched_entity_with_who_made_the_call(self):
        watched = set(THERMOSTATS.values())
        records = sig.call_records(
            self.event([THERMOSTATS["family"], "climate.bedroom_thermostat"], temperature=22),
            T0,
            watched,
        )
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(
            (r.entity, r.service, r.user_id, r.parent_id, r.ts),
            (THERMOSTATS["family"], "set_temperature", MIKKEL, None, T0),
        )
        self.assertEqual(r.data["temperature"], 22)

    def test_entity_ids_may_be_a_string_a_list_or_a_comma_separated_string(self):
        watched = set(THERMOSTATS.values())
        both = f"{THERMOSTATS['family']}, {THERMOSTATS['kristines_room']}"
        self.assertEqual(len(sig.call_records(self.event(both), T0, watched)), 2)
        self.assertEqual(
            len(sig.call_records(self.event(list(THERMOSTATS.values())), T0, watched)), 3
        )
        self.assertEqual(len(sig.call_records(self.event(THERMOSTATS["family"]), T0, watched)), 1)

    def test_an_unwatched_target_or_a_junk_event_gives_nothing(self):
        watched = set(THERMOSTATS.values())
        self.assertEqual(
            sig.call_records(self.event("climate.bedroom_thermostat"), T0, watched), []
        )
        for junk in (None, "x", {}, {"service_data": "x"}, {"service_data": {"entity_id": 5}}):
            self.assertEqual(sig.call_records(junk, T0, watched), [], junk)

    def test_an_event_without_context_has_no_account(self):
        event = self.event(THERMOSTATS["family"])
        for metadata in (None, {}, {"context": None}, "x"):
            event["metadata"] = metadata
            (r,) = sig.call_records(event, T0, set(THERMOSTATS.values()))
            self.assertEqual((r.user_id, r.parent_id), (None, None))


class CallWindow(unittest.TestCase):
    def test_calls_older_than_ten_minutes_are_dropped(self):
        b = buffer(call(T0), call(T0 + 100))
        self.assertEqual(len(b), 2)
        b.add(call(T0 + 601))
        self.assertEqual(len(b), 2)

    def test_recent_calls_are_newest_first_and_limited_to_one_entity_and_two_minutes(self):
        b = buffer(
            call(T0, temperature=1),
            call(T0 + 60, temperature=2),
            call(T0 + 60, entity=THERMOSTATS["family"], temperature=3),
            call(T0 + 90, temperature=4),
        )
        found = b.recent(THERMOSTATS["kristines_room"], T0 + 120)
        self.assertEqual([c.data["temperature"] for c in found], [4, 2, 1])
        found = b.recent(THERMOSTATS["kristines_room"], T0 + 121)
        self.assertEqual([c.data["temperature"] for c in found], [4, 2])

    def test_a_call_that_arrives_a_moment_after_the_change_still_counts(self):
        b = buffer(call(T0 + 101))
        self.assertEqual(len(b.recent(THERMOSTATS["kristines_room"], T0 + 100)), 1)
        self.assertEqual(len(b.recent(THERMOSTATS["kristines_room"], T0 + 98)), 0)


class Attribution(unittest.TestCase):
    def who(self, change, *calls, zc=KRISTINES, own=None):
        return sig.attribute_change(zc, change, buffer(*calls), IDENTITY, own)

    def test_a_call_from_a_person_is_that_person_through_home_assistant(self):
        for user, name in ((MIKKEL, "mikkel"), (KRISTINE, "kristine"), (CLAUDIA, "claudia")):
            a = self.who(setpoint(22.0), call(T0, user=user, temperature=22.0))
            self.assertEqual((a.source, a.who), (sig.SRC_HA, name), name)
            self.assertEqual(a.call.service, "set_temperature")

    def test_a_call_from_the_app_account_is_another_app(self):
        a = self.who(setpoint(22.0), call(T0, user=APP, temperature=22.0))
        self.assertEqual((a.source, a.who), (sig.SRC_APP, "appdaemon"))

    def test_a_parent_context_without_a_user_is_an_automation(self):
        a = self.who(setpoint(22.0), call(T0, parent="p", temperature=22.0))
        self.assertEqual((a.source, a.who), (sig.SRC_AUTOMATION, None))

    def test_an_account_nobody_maps_and_a_call_without_context_are_unknown(self):
        a = self.who(setpoint(22.0), call(T0, user=STRANGER, temperature=22.0))
        self.assertEqual((a.source, a.who), (sig.SRC_UNKNOWN, None))
        a = self.who(setpoint(22.0), call(T0, temperature=22.0))
        self.assertEqual(a.source, sig.SRC_UNKNOWN)

    def test_no_call_is_the_keypad_credited_to_the_rooms_only_resident(self):
        a = self.who(setpoint(22.0), zc=KRISTINES)
        self.assertEqual((a.source, a.who, a.call), (sig.SRC_KEYPAD, "kristine", None))
        a = self.who(setpoint(22.0), zc=CLAUDIAS)
        self.assertEqual((a.source, a.who), (sig.SRC_KEYPAD, "claudia"))
        a = self.who(setpoint(22.0), zc=FAMILY)
        self.assertEqual((a.source, a.who), (sig.SRC_KEYPAD, None))

    def test_a_room_with_two_residents_credits_nobody_for_a_keypad_change(self):
        shared = hp.parse_config(
            {
                "season_entity": "input_boolean.s",
                "zones": {
                    "kristines_room": zone("kristines_room", residents=["kristine", "claudia"])
                },
            }
        ).zones["kristines_room"]
        self.assertIsNone(self.who(setpoint(22.0), zc=shared).who)

    def test_a_call_two_minutes_old_counts_and_a_call_older_does_not(self):
        change = setpoint(22.0, ts=T0 + 120)
        self.assertEqual(
            self.who(change, call(T0, user=MIKKEL, temperature=22.0)).source, sig.SRC_HA
        )
        change = setpoint(22.0, ts=T0 + 121)
        self.assertEqual(
            self.who(change, call(T0, user=MIKKEL, temperature=22.0)).source, sig.SRC_KEYPAD
        )

    def test_a_call_on_another_thermostat_does_not_count(self):
        other = call(T0, entity=THERMOSTATS["claudias_room"], user=MIKKEL, temperature=22.0)
        self.assertEqual(self.who(setpoint(22.0), other).source, sig.SRC_KEYPAD)

    def test_a_call_for_another_value_does_not_explain_the_change(self):
        a = self.who(setpoint(22.0), call(T0, user=MIKKEL, temperature=15.0))
        self.assertEqual(a.source, sig.SRC_KEYPAD)

    def test_the_device_step_is_allowed_for_when_it_rounds_what_it_was_given(self):
        for asked, expected in ((21.7, sig.SRC_HA), (21.9, sig.SRC_HA), (22.1, sig.SRC_KEYPAD)):
            a = self.who(setpoint(21.5, old=20.0), call(T0, user=MIKKEL, temperature=asked))
            self.assertEqual(a.source, expected, asked)

    def test_a_call_with_a_string_temperature_still_matches(self):
        a = self.who(setpoint(22.0), call(T0, user=MIKKEL, temperature="22.0"))
        self.assertEqual(a.source, sig.SRC_HA)

    def test_the_newest_explaining_call_wins(self):
        a = self.who(
            setpoint(22.0, ts=T0 + 40),
            call(T0, parent="p", temperature=22.0),
            call(T0 + 30, user=MIKKEL, temperature=22.0),
        )
        self.assertEqual((a.source, a.who), (sig.SRC_HA, "mikkel"))

    def test_a_newer_call_that_explains_nothing_is_skipped(self):
        a = self.who(
            setpoint(22.0, ts=T0 + 40),
            call(T0, user=KRISTINE, temperature=22.0),
            call(T0 + 30, "set_hvac_mode", user=MIKKEL, hvac_mode="heat"),
        )
        self.assertEqual((a.source, a.who), (sig.SRC_HA, "kristine"))

    def test_a_mode_change_is_matched_to_the_calls_that_set_a_mode(self):
        to_off = sig.Change(sig.KIND_MODE, "heat", "off", T0 + 5)
        for service, data in (
            ("set_hvac_mode", {"hvac_mode": "off"}),
            ("turn_off", {}),
            ("toggle", {}),
            ("set_temperature", {"hvac_mode": "off", "temperature": 15}),
        ):
            a = self.who(to_off, call(T0, service, user=MIKKEL, **data))
            self.assertEqual(a.source, sig.SRC_HA, service)
        to_heat = sig.Change(sig.KIND_MODE, "off", "heat", T0 + 5)
        self.assertEqual(self.who(to_heat, call(T0, "turn_on", user=MIKKEL)).source, sig.SRC_HA)
        self.assertEqual(
            self.who(to_heat, call(T0, "turn_off", user=MIKKEL)).source, sig.SRC_KEYPAD
        )
        wrong = call(T0, "set_hvac_mode", user=MIKKEL, hvac_mode="heat")
        self.assertEqual(self.who(to_off, wrong).source, sig.SRC_KEYPAD)
        self.assertEqual(
            self.who(to_off, call(T0, "set_preset_mode", user=MIKKEL, preset_mode="x")).source,
            sig.SRC_KEYPAD,
        )

    def test_a_preset_change_is_matched_to_the_call_that_set_it(self):
        change = sig.Change(sig.KIND_PRESET, "permanent_hold", "schedule", T0 + 5)
        ok = call(T0, "set_preset_mode", parent="p", preset_mode="schedule")
        self.assertEqual(self.who(change, ok).source, sig.SRC_AUTOMATION)
        other = call(T0, "set_preset_mode", parent="p", preset_mode="boost")
        self.assertEqual(self.who(change, other).source, sig.SRC_KEYPAD)

    def test_the_controllers_own_write_is_excluded_first(self):
        own = sig.OwnWrite(22.0, T0, prev_reported=21.0)
        a = self.who(
            setpoint(22.0, ts=T0 + 5), call(T0 + 1, user=MIKKEL, temperature=22.0), own=own
        )
        self.assertEqual(a.source, sig.SRC_OWN)
        a = self.who(setpoint(22.0, ts=T0 + 600), own=own)
        self.assertEqual(a.source, sig.SRC_OWN)
        a = self.who(setpoint(22.0, ts=T0 + 601), own=own)
        self.assertEqual(a.source, sig.SRC_KEYPAD)

    def test_a_report_that_falls_back_to_the_value_before_the_write_is_a_device_rejection(self):
        own = sig.OwnWrite(22.0, T0, prev_reported=21.0)
        self.assertEqual(
            self.who(setpoint(21.0, ts=T0 + 120, old=22.0), own=own).source, sig.SRC_REVERT
        )
        self.assertEqual(
            self.who(setpoint(21.0, ts=T0 + 900, old=22.0), own=own).source, sig.SRC_REVERT
        )
        self.assertEqual(
            self.who(setpoint(21.0, ts=T0 + 901, old=22.0), own=own).source, sig.SRC_KEYPAD
        )

    def test_another_value_after_an_own_write_is_somebody_elses(self):
        own = sig.OwnWrite(22.0, T0, prev_reported=21.0)
        a = self.who(
            setpoint(23.0, ts=T0 + 30), call(T0 + 20, user=MIKKEL, temperature=23.0), own=own
        )
        self.assertEqual((a.source, a.who), (sig.SRC_HA, "mikkel"))

    def test_a_write_by_the_controller_does_not_hide_a_mode_change(self):
        own = sig.OwnWrite(22.0, T0, prev_reported=21.0)
        change = sig.Change(sig.KIND_MODE, "heat", "off", T0 + 5)
        self.assertEqual(self.who(change, own=own).source, sig.SRC_KEYPAD)


class ChangeDirection(unittest.TestCase):
    def test_only_the_sign_of_a_setpoint_or_target_change_counts(self):
        self.assertEqual(setpoint(22.0, old=21.5).direction, 1)
        self.assertEqual(setpoint(25.0, old=21.5).direction, 1)
        self.assertEqual(setpoint(21.0, old=21.5).direction, -1)
        self.assertEqual(setpoint(21.5, old=21.5).direction, 0)
        self.assertEqual(sig.Change(sig.KIND_TARGET, "21.5", "22.0", T0).direction, 1)

    def test_other_changes_and_unreadable_numbers_have_no_direction(self):
        self.assertEqual(sig.Change(sig.KIND_MODE, "heat", "off", T0).direction, 0)
        self.assertEqual(sig.Change(sig.KIND_PRESET, "a", "b", T0).direction, 0)
        self.assertEqual(sig.Change(sig.KIND_HELPER, "1", "2", T0).direction, 0)
        self.assertEqual(sig.Change(sig.KIND_TARGET, "x", "22", T0).direction, 0)
        self.assertEqual(sig.Change(sig.KIND_SETPOINT, None, 22.0, T0).direction, 0)


class ThermostatEffects(unittest.TestCase):
    def effect(self, zc, change, source, who=None):
        return sig.thermostat_effect(zc, "mikkel", change, sig.Attribution(source, who))

    def test_a_residents_setpoint_change_in_her_own_room_is_a_nudge(self):
        up = setpoint(22.0)
        self.assertEqual(self.effect(KRISTINES, up, sig.SRC_HA, "kristine"), sig.EFFECT_NUDGE)
        self.assertEqual(self.effect(KRISTINES, up, sig.SRC_KEYPAD, "kristine"), sig.EFFECT_NUDGE)
        self.assertEqual(self.effect(CLAUDIAS, up, sig.SRC_HA, "claudia"), sig.EFFECT_NUDGE)
        self.assertEqual(self.effect(CLAUDIAS, up, sig.SRC_KEYPAD, "claudia"), sig.EFFECT_NUDGE)

    def test_the_owner_moving_a_managed_thermostat_moves_the_target_a_step(self):
        for zc in (FAMILY, KRISTINES, CLAUDIAS):
            self.assertEqual(
                self.effect(zc, setpoint(22.0), sig.SRC_HA, "mikkel"), sig.EFFECT_WOULD_SET_TARGET
            )

    def test_everybody_else_is_restored_and_leaves_the_target_alone(self):
        up = setpoint(22.0)
        restore = sig.EFFECT_WOULD_RESTORE
        self.assertEqual(self.effect(KRISTINES, up, sig.SRC_HA, "claudia"), restore)
        self.assertEqual(self.effect(CLAUDIAS, up, sig.SRC_HA, "kristine"), restore)
        self.assertEqual(self.effect(KRISTINES, up, sig.SRC_APP, "appdaemon"), restore)
        self.assertEqual(self.effect(KRISTINES, up, sig.SRC_AUTOMATION), restore)
        self.assertEqual(self.effect(KRISTINES, up, sig.SRC_UNKNOWN), restore)
        self.assertEqual(self.effect(KRISTINES, up, sig.SRC_HA, "stranger"), restore)

    def test_a_keypad_change_on_the_shared_thermostat_is_restored(self):
        self.assertEqual(
            self.effect(FAMILY, setpoint(22.0), sig.SRC_KEYPAD), sig.EFFECT_WOULD_RESTORE
        )
        self.assertEqual(
            self.effect(FAMILY, setpoint(22.0), sig.SRC_HA, "kristine"), sig.EFFECT_WOULD_RESTORE
        )

    def test_switching_a_zone_off_is_the_owners_choice_and_nobody_elses(self):
        off = sig.Change(sig.KIND_MODE, "heat", "off", T0)
        self.assertEqual(self.effect(KRISTINES, off, sig.SRC_HA, "mikkel"), sig.EFFECT_OFF_BY_OWNER)
        for source, who in (
            (sig.SRC_HA, "kristine"),
            (sig.SRC_KEYPAD, "kristine"),
            (sig.SRC_APP, "appdaemon"),
            (sig.SRC_AUTOMATION, None),
        ):
            self.assertEqual(
                self.effect(KRISTINES, off, source, who), sig.EFFECT_WOULD_RESTORE, source
            )

    def test_turning_a_zone_on_needs_nothing(self):
        heat = sig.Change(sig.KIND_MODE, "off", "heat", T0)
        for source, who in (
            (sig.SRC_HA, "mikkel"),
            (sig.SRC_KEYPAD, "kristine"),
            (sig.SRC_APP, "appdaemon"),
        ):
            self.assertEqual(self.effect(KRISTINES, heat, source, who), sig.EFFECT_NONE)

    def test_a_preset_change_is_restored_unless_the_owner_made_it(self):
        preset = sig.Change(sig.KIND_PRESET, "permanent_hold", "schedule", T0)
        self.assertEqual(self.effect(KRISTINES, preset, sig.SRC_HA, "mikkel"), sig.EFFECT_NONE)
        self.assertEqual(
            self.effect(KRISTINES, preset, sig.SRC_KEYPAD, "kristine"), sig.EFFECT_WOULD_RESTORE
        )
        self.assertEqual(
            self.effect(KRISTINES, preset, sig.SRC_AUTOMATION), sig.EFFECT_WOULD_RESTORE
        )

    def test_the_controllers_own_writes_and_rejections_mean_nothing(self):
        for source in (sig.SRC_OWN, sig.SRC_REVERT):
            self.assertEqual(self.effect(KRISTINES, setpoint(22.0), source), sig.EFFECT_NONE)


class ButtonsAndHelpers(unittest.TestCase):
    def test_a_nudge_button_answers_to_the_rooms_residents_and_the_owner(self):
        person = lambda name: sig.Actor("person", name)  # noqa: E731
        self.assertTrue(sig.button_allowed(KRISTINES, "mikkel", person("kristine")))
        self.assertTrue(sig.button_allowed(KRISTINES, "mikkel", person("mikkel")))
        self.assertFalse(sig.button_allowed(KRISTINES, "mikkel", person("claudia")))
        self.assertTrue(sig.button_allowed(CLAUDIAS, "mikkel", person("claudia")))
        for actor in (
            sig.Actor("app", "appdaemon"),
            sig.Actor("automation"),
            sig.Actor("unknown_user"),
            sig.Actor("none"),
        ):
            self.assertFalse(sig.button_allowed(KRISTINES, "mikkel", actor), actor)

    def test_the_owner_changing_a_residents_target_resets_her_nudge(self):
        owner = sig.Actor("person", "mikkel")
        self.assertEqual(sig.helper_effect("mikkel", owner, KRISTINES, 0.5), sig.EFFECT_NUDGE_RESET)
        self.assertEqual(
            sig.helper_effect("mikkel", owner, KRISTINES, -1.0), sig.EFFECT_NUDGE_RESET
        )
        self.assertEqual(sig.helper_effect("mikkel", owner, KRISTINES, 0.0), sig.EFFECT_TARGET_SET)

    def test_the_owner_changing_any_other_target_just_sets_it(self):
        owner = sig.Actor("person", "mikkel")
        self.assertEqual(sig.helper_effect("mikkel", owner, FAMILY, 0.0), sig.EFFECT_TARGET_SET)
        self.assertEqual(sig.helper_effect("mikkel", owner, FAMILY, 0.5), sig.EFFECT_TARGET_SET)

    def test_the_owner_changing_a_helper_that_is_not_a_target_is_not_news(self):
        self.assertEqual(
            sig.helper_effect("mikkel", sig.Actor("person", "mikkel")), sig.EFFECT_NONE
        )

    def test_an_app_may_change_the_owners_helpers_without_resetting_a_nudge(self):
        app = sig.Actor("app", "appdaemon")
        self.assertEqual(sig.helper_effect("mikkel", app, KRISTINES, 0.5), sig.EFFECT_TARGET_SET)
        self.assertEqual(sig.helper_effect("mikkel", app), sig.EFFECT_NONE)

    def test_anybody_else_changing_the_owners_helper_would_be_reverted(self):
        for actor in (
            sig.Actor("person", "kristine"),
            sig.Actor("person", "claudia"),
            sig.Actor("automation"),
            sig.Actor("unknown_user"),
        ):
            self.assertEqual(
                sig.helper_effect("mikkel", actor, KRISTINES, 0.0), sig.EFFECT_WOULD_REVERT, actor
            )
            self.assertEqual(sig.helper_effect("mikkel", actor), sig.EFFECT_WOULD_REVERT, actor)

    def test_a_change_with_no_context_is_home_assistant_itself_and_is_left_alone(self):
        nobody = sig.Actor("none")
        self.assertEqual(sig.helper_effect("mikkel", nobody, FAMILY), sig.EFFECT_TARGET_SET)
        self.assertEqual(sig.helper_effect("mikkel", nobody), sig.EFFECT_NONE)


class NudgeSteps(unittest.TestCase):
    NC = hp.NudgeConfig(-1.0, 1.0, 0.5)

    def test_one_step_each_way(self):
        self.assertEqual(sig.step_nudge(self.NC, 0.0, 1, T0, None, 0), (0.5, sig.EFFECT_NUDGE))
        self.assertEqual(sig.step_nudge(self.NC, 0.0, -1, T0, None, 0), (-0.5, sig.EFFECT_NUDGE))
        self.assertEqual(
            sig.step_nudge(self.NC, 0.5, -1, T0 + 1000, T0, 1), (0.0, sig.EFFECT_NUDGE)
        )

    def test_the_nudge_stays_inside_its_bounds(self):
        self.assertEqual(sig.step_nudge(self.NC, 0.5, 1, T0, None, 0), (1.0, sig.EFFECT_NUDGE))
        self.assertEqual(
            sig.step_nudge(self.NC, 1.0, 1, T0, None, 0), (1.0, sig.EFFECT_NUDGE_LIMIT)
        )
        self.assertEqual(
            sig.step_nudge(self.NC, -1.0, -1, T0, None, 0), (-1.0, sig.EFFECT_NUDGE_LIMIT)
        )
        narrow = hp.NudgeConfig(-0.25, 0.25, 0.5)
        self.assertEqual(sig.step_nudge(narrow, 0.0, 1, T0, None, 0), (0.25, sig.EFFECT_NUDGE))

    def test_signals_in_the_same_direction_within_five_minutes_are_one_step(self):
        self.assertEqual(
            sig.step_nudge(self.NC, 0.5, 1, T0 + 299, T0, 1), (0.5, sig.EFFECT_NUDGE_COLLAPSED)
        )
        self.assertEqual(sig.step_nudge(self.NC, 0.5, 1, T0 + 300, T0, 1), (1.0, sig.EFFECT_NUDGE))

    def test_a_signal_the_other_way_is_a_new_step_even_inside_the_window(self):
        self.assertEqual(sig.step_nudge(self.NC, 0.5, -1, T0 + 10, T0, 1), (0.0, sig.EFFECT_NUDGE))
        self.assertEqual(
            sig.step_nudge(self.NC, 0.0, -1, T0 + 20, T0 + 10, -1),
            (0.0, sig.EFFECT_NUDGE_COLLAPSED),
        )

    def test_a_press_at_the_limit_still_anchors_the_window(self):
        self.assertEqual(
            sig.step_nudge(self.NC, 1.0, 1, T0 + 60, T0, 1), (1.0, sig.EFFECT_NUDGE_COLLAPSED)
        )

    def test_a_clock_that_went_backwards_does_not_collapse(self):
        self.assertEqual(sig.step_nudge(self.NC, 0.0, 1, T0 - 50, T0, 1), (0.5, sig.EFFECT_NUDGE))

    def test_no_direction_is_no_step(self):
        self.assertEqual(sig.step_nudge(self.NC, 0.5, 0, T0, None, 0), (0.5, sig.EFFECT_NONE))

    def test_repeated_small_steps_do_not_drift(self):
        fine = hp.NudgeConfig(-1.0, 1.0, 0.1)
        nudge, last = 0.0, None
        for i in range(10):
            nudge, effect = sig.step_nudge(fine, nudge, 1, T0 + 1000 * i, last, 1)
            last = T0 + 1000 * i
            self.assertEqual(effect, sig.EFFECT_NUDGE)
        self.assertEqual(nudge, 1.0)

    def test_a_zone_without_a_nudge_range_never_moves(self):
        none = hp.NudgeConfig()
        self.assertEqual(sig.step_nudge(none, 0.0, 1, T0, None, 0), (0.0, sig.EFFECT_NUDGE_LIMIT))


class Module(unittest.TestCase):
    def test_it_is_pure(self):
        source = (Path(__file__).resolve().parents[1] / "heating_signals.py").read_text()
        self.assertIsNone(re.search(r"^\s*(import|from)\s+appdaemon", source, re.M))
        self.assertNotIn("open(", source)

    def test_the_service_call_event_and_domain_are_the_climate_ones(self):
        self.assertEqual((sig.CALL_EVENT, sig.CALL_DOMAINS), ("call_service", ("climate",)))


if __name__ == "__main__":
    unittest.main()
