"""Unit tests for DishwasherIslandSignal ownership and hand-back.

The signal owns the island only while the dishwasher is Unemptied AND the kitchen is occupied. When it ends
(kitchen clears, or the dishwasher leaves Unemptied) the island goes back to FamilyRoomLights: no light is
powered on, a full group the signal itself powered up from off in the bright path is switched off, and every
other lit bulb is only recolored to normal. The AL layout restore and the recolor run after a settle delay so
they cannot re-light a group FamilyRoomLights has just switched off (AL adapts lights HA reports as on, and HA's
state for a Zigbee group trails the command).

Regression (2026-07-24 19:10): opening the dishwasher (Unemptied -> Emptied) in a bright family room turned the
island off and then straight back on white for ~6 minutes. Cause: right after turn_off(full group),
get_state(bulb1) still read "on" (stale cache), so cleanup ran adaptive_lighting/apply with turn_on_lights=True.
No hand-back may ever use turn_on_lights=True.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

import dishwasher_island_signal  # noqa: E402

DISHWASHER = "sensor.dishwasher_state"
PIR = "binary_sensor.kitchen_pir_presence"
BULB1 = "light.island_light_1"
FULL = "light.island_lights"
SG_LIGHT = "light.island_lights_sg"
ROOM_STATE = "sensor.room_state_family_room"
DARK_SENSOR = "sensor.darkness_family_room"
AL_MAIN = "switch.adaptive_lighting_island_lights"
AL_SG = "switch.adaptive_lighting_island_light_sg"


def make_app(states):
    app = dishwasher_island_signal.DishwasherIslandSignal.__new__(
        dishwasher_island_signal.DishwasherIslandSignal
    )
    app._dishwasher = DISHWASHER
    app._pir = PIR
    app._signal_light = BULB1
    app._full_island = FULL
    app._island_sg_light = SG_LIGHT
    app._room_state = ROOM_STATE
    app._al_main = AL_MAIN
    app._al_sg = AL_SG
    app._unemptied = "Unemptied"
    app._brightness = 100
    app._hs = [120, 100]
    app._island_powered_by_signal = False
    app.args = {"darkness_confirmed_sensor_entity": DARK_SENSOR}

    app.states = dict(states)
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.log = lambda *a, **kw: None
    app.call_service = MagicMock()
    app.turn_on = MagicMock()
    app.turn_off = MagicMock()
    app.run_in = MagicMock()
    return app


def states_for(
    *,
    dishwasher="Unemptied",
    pir="off",
    dark=True,
    full="off",
    bulb1="off",
    sg="off",
    al_main="on",
    al_sg="off",
):
    """HA state of everything the app reads; defaults to the normal layout with the island off."""
    return {
        DISHWASHER: dishwasher,
        PIR: pir,
        FULL: full,
        BULB1: bulb1,
        SG_LIGHT: sg,
        AL_MAIN: al_main,
        AL_SG: al_sg,
        DARK_SENSOR: "dark" if dark else "bright",
        ROOM_STATE: "Occupied (Dark)" if dark else "Occupied (Bright)",
    }


def dark_solo_states(**overrides):
    """Dark-solo layout as HA reports it: SG group + bulb1 lit (full group reads on), AL main off, SG on."""
    base = dict(full="on", bulb1="on", sg="on", al_main="off", al_sg="on")
    base.update(overrides)
    return states_for(**base)


def apply_calls(app):
    return [
        c for c in app.call_service.call_args_list if c.args[0] == "adaptive_lighting/apply"
    ]


def turn_on_lights_calls(app):
    return [c for c in apply_calls(app) if c.kwargs.get("turn_on_lights")]


def manual_control_calls(app):
    return [
        (c.kwargs["entity_id"], c.kwargs["lights"], c.kwargs["manual_control"])
        for c in app.call_service.call_args_list
        if c.args[0] == "adaptive_lighting/set_manual_control"
    ]


def turned_off(app):
    return [c.args[0] for c in app.turn_off.call_args_list]


def turned_on(app):
    return [c.args[0] for c in app.turn_on.call_args_list]


def reset_calls(app):
    for mock in (app.call_service, app.turn_on, app.turn_off, app.run_in):
        mock.reset_mock()


def settle(app):
    """Run the hand-back timers the app scheduled with run_in (the delay itself is not simulated)."""
    scheduled = [c.args for c in app.run_in.call_args_list]
    app.run_in.reset_mock()
    for callback, delay in scheduled:
        assert delay == dishwasher_island_signal._HAND_BACK_SETTLE_S
        callback({})


def assert_untouched(app):
    app.turn_on.assert_not_called()
    app.turn_off.assert_not_called()
    app.call_service.assert_not_called()


class SignalOwnership(unittest.TestCase):
    """Which island state the signal itself is responsible for."""

    def test_bright_signal_from_off_marks_the_island_as_powered(self):
        app = make_app(states_for(pir="on", dark=False))
        app._sync_signal()
        self.assertEqual(turned_on(app), [FULL])
        self.assertTrue(app._island_powered_by_signal)

    def test_bright_signal_over_a_lit_island_only_recolors_it(self):
        app = make_app(states_for(pir="on", dark=False, full="on", bulb1="on", sg="on"))
        app._sync_signal()
        self.assertEqual(turned_on(app), [FULL])
        self.assertFalse(app._island_powered_by_signal)

    def test_bright_reapply_keeps_the_powered_mark(self):
        app = make_app(states_for(pir="on", dark=False))
        app._sync_signal()
        app.states.update({FULL: "on", BULB1: "on", AL_MAIN: "off"})
        app._sync_signal()
        self.assertTrue(app._island_powered_by_signal)

    def test_dark_solo_signal_drops_the_powered_mark(self):
        app = make_app(states_for(pir="on", dark=False))
        app._sync_signal()
        self.assertTrue(app._island_powered_by_signal)
        app.states.update(
            {
                DARK_SENSOR: "dark",
                ROOM_STATE: "Occupied (Dark)",
                FULL: "on",
                BULB1: "on",
                AL_MAIN: "off",
            }
        )
        app._sync_signal()
        self.assertFalse(app._island_powered_by_signal)

    def test_dark_solo_resync_does_not_blink_the_island(self):
        app = make_app(dark_solo_states(pir="on"))
        app._sync_signal()
        assert_untouched(app)

    def test_entering_dark_solo_never_switches_a_lit_island_off(self):
        app = make_app(states_for(pir="on", full="on", bulb1="on", sg="on"))
        app._sync_signal()
        self.assertEqual(turned_off(app), [AL_MAIN])
        self.assertIn(BULB1, turned_on(app))

    def test_dark_solo_from_the_bright_layout_is_applied(self):
        app = make_app(states_for(pir="on", full="on", bulb1="on", sg="on", al_main="off", al_sg="off"))
        app._sync_signal()
        self.assertIn(AL_SG, turned_on(app))


class KitchenClearsDuringDarkSolo(unittest.TestCase):
    def test_nothing_is_touched_until_the_settle_delay_has_passed(self):
        app = make_app(dark_solo_states())
        app._on_pir(PIR, None, "on", "off", {})
        assert_untouched(app)
        self.assertEqual(len(app.run_in.call_args_list), 1)

    def test_hand_back_restores_the_layout_and_recolors_without_switching_a_light(self):
        app = make_app(dark_solo_states())
        app._on_pir(PIR, None, "on", "off", {})
        settle(app)

        # Only the two AL switches are toggled - no light is switched off or on.
        self.assertEqual(turned_off(app), [AL_SG])
        self.assertEqual(turned_on(app), [AL_MAIN])
        self.assertCountEqual(
            manual_control_calls(app),
            [(AL_SG, [BULB1], False), (AL_MAIN, [BULB1], False)],
        )
        applies = apply_calls(app)
        self.assertEqual(len(applies), 1)
        self.assertEqual(
            applies[0].kwargs,
            {"entity_id": AL_MAIN, "lights": [FULL], "turn_on_lights": False},
        )

    def test_island_lit_by_family_room_stays_lit_when_the_kitchen_clears(self):
        """FamilyRoomLights had the island on, the kitchen signal took it over in the dark, the user sits down
        in the dining room and the kitchen clears: the island must stay on in its normal layout."""
        app = make_app(states_for(pir="on", full="on", bulb1="on", sg="on"))
        app._sync_signal()
        app.states.update(dark_solo_states(pir="on"))
        reset_calls(app)

        app.states[PIR] = "off"
        app._on_pir(PIR, None, "on", "off", {})
        settle(app)

        for light in (FULL, SG_LIGHT, BULB1):
            self.assertNotIn(light, turned_off(app))
        self.assertEqual(turned_on(app), [AL_MAIN])
        self.assertEqual(turn_on_lights_calls(app), [])
        self.assertEqual(len(apply_calls(app)), 1)

    def test_island_that_is_off_gets_the_layout_but_no_recolor(self):
        app = make_app(dark_solo_states(full="off", bulb1="off", sg="off"))
        app._on_pir(PIR, None, "on", "off", {})
        settle(app)

        self.assertEqual(turned_off(app), [AL_SG])
        self.assertEqual(turned_on(app), [AL_MAIN])
        self.assertEqual(apply_calls(app), [])

    def test_signal_coming_back_before_the_delay_is_not_undone(self):
        app = make_app(dark_solo_states())
        app._on_pir(PIR, None, "on", "off", {})
        app.states[PIR] = "on"
        settle(app)
        assert_untouched(app)


class KitchenClearsDuringBrightSignal(unittest.TestCase):
    def _signalled(self, island_lit_before):
        """Run the real bright apply, then report HA's state after it and clear the kitchen."""
        lit = "on" if island_lit_before else "off"
        app = make_app(states_for(pir="on", dark=False, full=lit, bulb1=lit, sg=lit))
        app._sync_signal()
        app.states.update({FULL: "on", BULB1: "on", AL_MAIN: "off", AL_SG: "off", PIR: "off"})
        reset_calls(app)
        return app

    def test_group_the_signal_powered_up_is_switched_off(self):
        app = self._signalled(island_lit_before=False)
        app._on_pir(PIR, None, "on", "off", {})

        self.assertEqual(turned_off(app), [FULL, BULB1])
        self.assertEqual(turned_on(app), [])
        self.assertFalse(app._island_powered_by_signal)

        # By the time the settle delay has passed HA reports the island off: layout only, no recolor.
        app.states.update({FULL: "off", BULB1: "off"})
        settle(app)
        self.assertEqual(turned_on(app), [AL_MAIN])
        self.assertEqual(apply_calls(app), [])

    def test_island_that_was_already_lit_is_recolored_not_switched_off(self):
        app = self._signalled(island_lit_before=True)
        app._on_pir(PIR, None, "on", "off", {})
        settle(app)

        self.assertEqual(turned_off(app), [])
        self.assertEqual(turned_on(app), [AL_MAIN])
        applies = apply_calls(app)
        self.assertEqual(len(applies), 1)
        self.assertEqual(applies[0].kwargs["lights"], [FULL])
        self.assertFalse(applies[0].kwargs["turn_on_lights"])


class NothingToHandBack(unittest.TestCase):
    """Unemptied with the kitchen empty is the normal layout: the app leaves the island to FamilyRoomLights."""

    def test_room_state_change_with_an_empty_kitchen_touches_nothing(self):
        app = make_app(states_for(pir="off", full="on", bulb1="on", sg="on"))
        app._on_room_state(ROOM_STATE, None, "Occupied (Dark)", "Occupied (Bright)", {})
        assert_untouched(app)
        app.run_in.assert_not_called()

    def test_room_state_change_with_an_empty_kitchen_ignores_even_a_leftover_signal_layout(self):
        app = make_app(dark_solo_states())
        app._on_room_state(ROOM_STATE, None, "Occupied (Dark)", "Occupied (Bright)", {})
        assert_untouched(app)
        app.run_in.assert_not_called()

    def test_entering_unemptied_with_an_empty_kitchen_touches_nothing(self):
        app = make_app(states_for(pir="off", full="on", bulb1="on", sg="on"))
        app._on_dishwasher_state(DISHWASHER, None, "Off", "Unemptied", {})
        settle(app)
        assert_untouched(app)

    def test_kitchen_clearing_without_a_signal_layout_touches_nothing(self):
        """E.g. the signal was never applied (suspect presence): nothing to restore, nothing to recolor."""
        app = make_app(states_for(pir="off", full="on", bulb1="on", sg="on"))
        app._on_pir(PIR, None, "on", "off", {})
        settle(app)
        assert_untouched(app)

    def test_pir_change_while_not_unemptied_is_ignored(self):
        app = make_app(states_for(dishwasher="Emptied", pir="off", full="on"))
        app._on_pir(PIR, None, "on", "off", {})
        assert_untouched(app)
        app.run_in.assert_not_called()


class StartupSync(unittest.TestCase):
    def test_unemptied_with_an_empty_kitchen_restores_the_layout_without_power_changes(self):
        app = make_app(dark_solo_states())
        app._startup_sync({})
        assert_untouched(app)
        settle(app)

        self.assertEqual(turned_off(app), [AL_SG])
        self.assertEqual(turned_on(app), [AL_MAIN])
        for light in (FULL, SG_LIGHT, BULB1):
            self.assertNotIn(light, turned_off(app) + turned_on(app))

    def test_unemptied_with_the_normal_layout_touches_nothing(self):
        app = make_app(states_for(pir="off", full="on", bulb1="on", sg="on"))
        app._startup_sync({})
        settle(app)
        assert_untouched(app)

    def test_not_unemptied_with_a_leftover_signal_layout_is_reconciled(self):
        app = make_app(dark_solo_states(dishwasher="Emptied"))
        app._startup_sync({})
        settle(app)

        self.assertEqual(turned_off(app), [AL_SG])
        self.assertEqual(turned_on(app), [AL_MAIN])
        self.assertEqual(turn_on_lights_calls(app), [])


class LeavingUnemptiedBright(unittest.TestCase):
    def _bright_green_states(self):
        """Full-group green signal active; bulb1 STILL reads on (stale) after group off."""
        return {
            DISHWASHER: "Emptied",
            PIR: "on",
            FULL: "on",
            BULB1: "on",
            SG_LIGHT: "off",
            AL_MAIN: "off",
            AL_SG: "off",
            DARK_SENSOR: "bright",
            ROOM_STATE: "Occupied (Bright)",
        }

    def test_door_open_while_bright_green_ends_all_off(self):
        app = make_app(self._bright_green_states())
        app._island_powered_by_signal = True

        app._on_dishwasher_state(DISHWASHER, None, "Unemptied", "Emptied", {})

        self.assertIn(((FULL,), {}), app.turn_off.call_args_list)
        self.assertEqual(turned_on(app), [])
        self.assertFalse(app._island_powered_by_signal)

        settle(app)
        # The regression: stale bulb1 "on" must NOT trigger a turn_on_lights hand-back.
        self.assertEqual(turn_on_lights_calls(app), [])
        # AL layout restored to normal (main on); no light entity is turned on.
        self.assertEqual(app.turn_on.call_args_list, [((AL_MAIN,), {})])

    def test_bright_room_without_a_signal_powered_island_is_recolored_not_cleared(self):
        """Dark-solo layout but the room meanwhile reads confirmed bright, and the group was not powered up
        by the signal: the island is not ours to switch off - only the layout and the colour are restored."""
        states = self._bright_green_states()
        states.update({FULL: "off", BULB1: "on", SG_LIGHT: "on", AL_SG: "on"})
        app = make_app(states)
        app._island_powered_by_signal = False

        app._on_dishwasher_state(DISHWASHER, None, "Unemptied", "Emptied", {})
        settle(app)

        self.assertEqual(turn_on_lights_calls(app), [])
        self.assertEqual(app.turn_off.call_args_list, [((AL_SG,), {})])
        self.assertEqual(app.turn_on.call_args_list, [((AL_MAIN,), {})])
        applies = apply_calls(app)
        self.assertEqual(len(applies), 1)
        self.assertEqual(applies[0].kwargs["entity_id"], AL_MAIN)
        self.assertFalse(applies[0].kwargs["turn_on_lights"])


class LeavingUnemptiedDark(unittest.TestCase):
    def test_dark_solo_hand_back_recolors_through_main_al_and_keeps_the_island_lit(self):
        """Dark path: bulb1 is recolored by main AL (turn_on_lights=False); no light is switched off."""
        app = make_app(
            {
                DISHWASHER: "Emptied",
                PIR: "on",
                FULL: "off",
                BULB1: "on",
                SG_LIGHT: "on",
                AL_MAIN: "off",
                AL_SG: "on",
                DARK_SENSOR: "dark",
                ROOM_STATE: "Occupied (Dark)",
            }
        )
        app._island_powered_by_signal = False

        app._on_dishwasher_state(DISHWASHER, None, "Unemptied", "Emptied", {})
        settle(app)

        applies = apply_calls(app)
        self.assertEqual(len(applies), 1)
        self.assertEqual(applies[0].kwargs["entity_id"], AL_MAIN)
        self.assertEqual(applies[0].kwargs["lights"], [FULL])
        self.assertFalse(applies[0].kwargs["turn_on_lights"])
        self.assertEqual(turned_off(app), [AL_SG])
        self.assertIn(AL_MAIN, turned_on(app))


if __name__ == "__main__":
    unittest.main()
