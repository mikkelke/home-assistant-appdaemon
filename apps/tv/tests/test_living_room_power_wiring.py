import unittest

from test_living_room_apple_tv_disconnect import APPLE, HISENSE, REMOTE, TV, make_app, states


LOCAL_TV = "media_player.hisense_vidaa_local"
SOURCE_SCRIPT = "script.living_room_tv_select_source"
POWER_ON = ("media_player/turn_on", {"entity_id": TV})
CONNECT = ("remote/turn_on", {"entity_id": REMOTE})
DISCONNECT = ("remote/turn_off", {"entity_id": REMOTE})


def configured_app(source=None, **overrides):
    app = make_app(states(**{LOCAL_TV: "on", TV: "on", **overrides}), {
        "hisense_tv_entity": LOCAL_TV,
        "apple_tv_source_name": "HDMI2",
        "apple_tv_reset_enabled": False,
    })

    def get_state(entity, attribute=None, **kwargs):
        if entity == LOCAL_TV and attribute == "source":
            return source
        return app.states.get(entity)

    app.get_state = get_state
    return app


class SharedPowerOnWiring(unittest.TestCase):
    def test_off_tv_gets_one_aggregate_request_without_direct_fallbacks(self):
        app = configured_app(**{TV: "off", LOCAL_TV: "off"})
        app._power_on_tv_if_needed("physical button")
        self.assertEqual(app.service_calls, [POWER_ON])

    def test_unknown_tv_still_uses_only_the_shared_request(self):
        app = configured_app(**{TV: "unknown", LOCAL_TV: "unavailable"})
        app._power_on_tv_if_needed("manual lift")
        self.assertEqual(app.service_calls, [POWER_ON])

    def test_failed_shared_request_does_not_send_blind_direct_commands(self):
        app = configured_app(**{TV: "off", LOCAL_TV: "off"})

        def unavailable(service, **kwargs):
            app.service_calls.append((service, kwargs))
            raise RuntimeError("HA unavailable")

        app.call_service = unavailable
        app._power_on_tv_if_needed("physical button")
        self.assertEqual(app.service_calls, [POWER_ON])

    def test_already_on_tv_preserves_the_selected_input(self):
        app = configured_app(source="HDMI1")
        app._power_on_tv_if_needed("physical button")
        self.assertEqual(app.service_calls, [])


class AppleTvSourceGate(unittest.TestCase):
    def test_source_listener_is_registered_only_when_configured(self):
        app = configured_app()
        self.assertIn(
            (app._on_tv_source_changed, LOCAL_TV, {"attribute": "source"}),
            app.listeners,
        )
        legacy = make_app(states())
        self.assertFalse(any(cb == legacy._on_tv_source_changed for cb, _, _ in legacy.listeners))

    def test_hdmi2_reconnects_the_disconnected_integration_only(self):
        app = configured_app(source="HDMI2")
        app._reconnect_apple_tv_on_init({})
        self.assertEqual(app.service_calls, [CONNECT])

    def test_other_or_unknown_sources_do_not_connect_a_sleeping_apple_tv(self):
        for source in ("Home", "HDMI1", "HDMI3", None, "unknown", "unavailable", ""):
            with self.subTest(source=source):
                app = configured_app(source=source)
                app._reconnect_apple_tv_if_needed("TV turned on")
                self.assertEqual(app.service_calls, [])
                self.assertIsNone(app._apple_reconnect_at)

    def test_source_is_read_from_the_configured_primary_provider(self):
        app = configured_app(source="Home")
        app.states[HISENSE] = "on"
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [])

    def test_off_tv_is_not_reconnected_even_with_a_retained_hdmi2_attribute(self):
        app = configured_app(source="HDMI2", **{TV: "off", LOCAL_TV: "off"})
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [])

    def test_unknown_primary_does_not_reconnect_with_a_retained_hdmi2_attribute(self):
        app = configured_app(source="HDMI2", **{LOCAL_TV: "unknown"})
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [])

    def test_source_read_failure_sends_nothing(self):
        app = configured_app(source="HDMI2")
        original_get_state = app.get_state

        def unavailable(entity, attribute=None, **kwargs):
            if attribute == "source":
                raise RuntimeError("source unavailable")
            return original_get_state(entity, attribute=attribute, **kwargs)

        app.get_state = unavailable
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [])

    def test_default_without_source_gate_preserves_existing_reconnect_behavior(self):
        app = make_app(states(**{TV: "on", HISENSE: "on", APPLE: "off", REMOTE: "off"}))
        self.assertIsNone(app.apple_tv_source_name)
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [CONNECT])


class AppleTvInputChanges(unittest.TestCase):
    def test_managed_selection_owns_the_connection_during_input_transitions(self):
        app = configured_app(source=None, **{REMOTE: "on", SOURCE_SCRIPT: "on"})
        app.source_selection_script_entity = SOURCE_SCRIPT
        app._on_tv_source_changed(LOCAL_TV, "source", "HDMI2", None, {})
        self.assertEqual(app.service_calls, [])
        app.states[SOURCE_SCRIPT] = "off"
        app._on_tv_source_changed(LOCAL_TV, "source", "HDMI2", None, {})
        self.assertEqual(app.service_calls, [DISCONNECT])

    def test_selection_script_configuration_is_optional(self):
        legacy = make_app(states())
        self.assertIsNone(legacy.source_selection_script_entity)
        configured = make_app(states(), {"source_selection_script_entity": SOURCE_SCRIPT})
        self.assertEqual(configured.source_selection_script_entity, SOURCE_SCRIPT)

    def test_leaving_hdmi2_disconnects_only_the_integration_once(self):
        for source in ("HDMI1", "Home", None):
            with self.subTest(source=source):
                app = configured_app(source=source, **{REMOTE: "on", APPLE: "playing"})
                app._on_tv_source_changed(LOCAL_TV, "source", "HDMI2", source, {})
                self.assertEqual(app.service_calls, [DISCONNECT])
                app.states[REMOTE] = "off"
                app._on_tv_source_changed(LOCAL_TV, "source", "HDMI2", source, {})
                self.assertEqual(app.service_calls, [DISCONNECT])

    def test_unknown_unavailable_or_off_primary_does_not_disconnect(self):
        for primary in (None, "unknown", "unavailable", "off"):
            with self.subTest(primary=primary):
                app = configured_app(source="HDMI1", **{LOCAL_TV: primary, REMOTE: "on"})
                app._on_tv_source_changed(LOCAL_TV, "source", "HDMI2", "HDMI1", {})
                self.assertEqual(app.service_calls, [])

    def test_initial_or_reload_source_report_does_not_disconnect(self):
        app = configured_app(source="HDMI1", **{REMOTE: "on"})
        app._on_tv_source_changed(LOCAL_TV, "source", None, "HDMI1", {})
        self.assertEqual(app.service_calls, [])

    def test_selecting_hdmi2_uses_the_existing_reconnect_cooldown(self):
        app = configured_app(source="HDMI2")
        app._on_tv_source_changed(LOCAL_TV, "source", "HDMI1", "HDMI2", {})
        app._on_tv_source_changed(LOCAL_TV, "source", "Home", "HDMI2", {})
        self.assertEqual(app.service_calls, [CONNECT])

    def test_same_input_or_disabled_gate_sends_nothing(self):
        app = configured_app(source="HDMI1", **{REMOTE: "on"})
        app._on_tv_source_changed(LOCAL_TV, "source", "HDMI1", "HDMI1", {})
        app.apple_tv_source_name = None
        app._on_tv_source_changed(LOCAL_TV, "source", "HDMI2", "HDMI1", {})
        self.assertEqual(app.service_calls, [])


if __name__ == "__main__":
    unittest.main()
