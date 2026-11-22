"""The six spots turn on together and hand back eligible lights to Adaptive Lighting."""

import unittest

from test_lighting_presence_unknown import bathroom_app


class BathroomAdaptiveGroup(unittest.TestCase):
    def setUp(self):
        self.app = bathroom_app(presence="off")
        self.app.adaptive_brightness_switch = "switch.adaptive_brightness"
        self.app.adaptive_color_switch = "switch.adaptive_color"
        self.app.states.update({
            "switch.adaptive": "on",
            "switch.adaptive_brightness": "on",
            "switch.adaptive_color": "on",
        })
        self.attributes = {
            "switch.adaptive": {
                "brightness_pct": 32,
                "color_temp_kelvin": 2000,
                "manual_control": [],
            },
            "light.all": {"min_color_temp_kelvin": 2202, "max_color_temp_kelvin": 4000},
        }
        self.app.get_state = self.get_state
        self.app.run_in.reset_mock()

    def get_state(self, entity, attribute=None):
        state = self.app.states.get(entity)
        attrs = self.attributes.get(entity, {})
        if attribute == "all":
            return {"state": state, "attributes": attrs}
        return attrs.get(attribute) if attribute else state

    def test_auto_on_uses_one_adapted_zigbee_group_command(self):
        self.app._on_raw_pir_on(None, None, "off", "on", {})
        self.app.turn_on.assert_called_once_with("light.all", brightness=82, color_temp_kelvin=2202)
        self.app.call_service.assert_not_called()

    def test_switch_on_uses_group_and_honors_adapt_color_toggle(self):
        self.app.states["switch.adaptive_color"] = "off"
        self.app._on_switch_event(None, {}, {})
        self.app.turn_on.assert_called_once_with("light.all", brightness=82)

    def test_switch_off_uses_all_spots_group_when_only_shower_is_on(self):
        self.app.states.update({"light.all": "on", "light.shower": "on"})
        self.app._on_switch_event(None, {}, {})
        self.app.turn_off.assert_called_once_with("light.all")
        self.app.turn_on.assert_not_called()

    def test_disabled_adaptive_lighting_uses_bare_group_turn_on(self):
        self.app.states["switch.adaptive"] = "off"
        self.app._turn_on_all()
        self.app.turn_on.assert_called_once_with("light.all")
        self.app.run_in.assert_not_called()

    def test_handoff_releases_matching_group_and_keeps_manual_adjustment(self):
        self.app._turn_on_all()
        callback = self.app.run_in.call_args.args[0]
        kwargs = self.app.run_in.call_args.kwargs
        self.app.states.update({"light.main": "on", "light.shower": "on"})
        self.attributes["switch.adaptive"]["manual_control"] = ["light.main", "light.shower"]
        self.attributes["light.main"] = {"brightness": 81, "color_temp_kelvin": 2203}
        self.attributes["light.shower"] = {"brightness": 100, "color_temp_kelvin": 2202}
        callback(kwargs)
        self.app.call_service.assert_called_once_with(
            "adaptive_lighting/set_manual_control",
            entity_id="switch.adaptive", lights=["light.main"], manual_control=False,
        )

    def test_handoff_skips_a_preexisting_manual_flag_or_on_spot(self):
        self.attributes["switch.adaptive"]["manual_control"] = ["light.main"]
        self.app.states["light.shower"] = "on"
        self.app._turn_on_all()
        self.app.run_in.assert_not_called()

    def test_bright_shower_only_path_uses_its_own_target(self):
        self.app.states.update({
            "sensor.darkness": "bright",
            "sensor.room_state": "Occupied (Bright)",
            "binary_sensor.shower": "on",
        })
        self.app._on_bath_presence_change(None, None, "off", "on", {})
        self.app.turn_on.assert_called_once_with("light.shower")
        self.app.run_in.assert_not_called()
