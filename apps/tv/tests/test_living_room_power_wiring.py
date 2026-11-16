import unittest
from pathlib import Path

import yaml

from test_living_room_apple_tv_disconnect import (
    APPLE, HISENSE, REMOTE, TV, fire, make_app, pending, states, tv_changes,
)


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

    app.source = source

    def get_state(entity, attribute=None, **kwargs):
        if entity == LOCAL_TV and attribute == "source":
            return app.source
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


def booting_app(**overrides):
    """TV drawing power while the local provider still cannot be reached."""
    app = configured_app(source=None, **{LOCAL_TV: "unavailable", TV: "unavailable", **overrides})
    app.power_off_threshold_w = 52.0
    app.power_w = 64.0
    app._tv_relay_power_suggests_tv_on = lambda: app.power_w > app.power_off_threshold_w
    app._evaluate_power_standby_wall = lambda: None
    app._read_tv_power_w = lambda: app.power_w
    return app


class AppleTvConnectsWhenTheTvDrawsPower(unittest.TestCase):
    def test_power_rise_connects_before_the_tv_reports_its_input(self):
        app = booting_app()
        app._on_relay_power_changed("sensor.p", "state", "3.6", "64.9", {})
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertTrue(app._apple_tv_input_unconfirmed)

    def test_power_already_above_threshold_does_not_retrigger(self):
        app = booting_app()
        app._on_relay_power_changed("sensor.p", "state", "70.0", "90.0", {})
        self.assertEqual(app.service_calls, [])

    def test_no_power_evidence_keeps_the_unknown_tv_unconnected(self):
        app = booting_app()
        app.power_w = 3.6
        app._reconnect_apple_tv_if_needed("startup")
        self.assertEqual(app.service_calls, [])

    def test_first_reported_other_input_disconnects(self):
        app = booting_app()
        app._reconnect_apple_tv_if_needed("TV drawing power")
        app.states.update({LOCAL_TV: "on", REMOTE: "on"})
        app._on_tv_source_changed(LOCAL_TV, "source", None, "HDMI1", {})
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])
        self.assertFalse(app._apple_tv_input_unconfirmed)

    def test_native_app_without_an_input_disconnects_when_the_wait_runs_out(self):
        app = booting_app()
        app._reconnect_apple_tv_if_needed("TV drawing power")
        app.states.update({LOCAL_TV: "on", REMOTE: "on"})
        app._on_hisense_reported_on(LOCAL_TV, "state", "unavailable", "on", {})
        self.assertEqual(app.service_calls, [CONNECT])
        fire(app, app._apple_tv_input_wait_due)
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])

    def test_apple_tv_input_keeps_the_early_connection(self):
        app = booting_app()
        app._reconnect_apple_tv_if_needed("TV drawing power")
        app.states.update({LOCAL_TV: "on", REMOTE: "on"})
        app._on_tv_source_changed(LOCAL_TV, "source", None, "HDMI2", {})
        app._on_hisense_reported_on(LOCAL_TV, "state", "unavailable", "on", {})
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertFalse(app._apple_tv_input_unconfirmed)

    def test_a_later_input_change_after_confirmation_uses_the_normal_rule(self):
        app = booting_app()
        app._reconnect_apple_tv_if_needed("TV drawing power")
        app.states.update({LOCAL_TV: "on", REMOTE: "on"})
        app._on_tv_source_changed(LOCAL_TV, "source", None, "HDMI2", {})
        app._on_hisense_reported_on(LOCAL_TV, "state", "off", "on", {})
        self.assertEqual(app.service_calls, [CONNECT])

    def test_elapsed_off_grace_does_not_cut_the_connection_while_the_tv_draws_power(self):
        app = booting_app()
        app._apple_tv_grace_elapsed = True
        app._on_apple_tv_remote_connected(REMOTE, "state", "off", "on", {})
        self.assertEqual(app.service_calls, [])

    def test_managed_input_selection_owns_the_early_connection(self):
        app = booting_app(**{SOURCE_SCRIPT: "on"})
        app.source_selection_script_entity = SOURCE_SCRIPT
        app._reconnect_apple_tv_if_needed("TV drawing power")
        app.states.update({LOCAL_TV: "on", REMOTE: "on"})
        app._on_tv_source_changed(LOCAL_TV, "source", None, "HDMI1", {})
        self.assertEqual(app.service_calls, [CONNECT])


def early_connected_app(**overrides):
    """Apple TV connected on the power rise; the provider is about to report on."""
    app = booting_app(**overrides)
    app.logs = []
    app.log = lambda message, **kwargs: app.logs.append(message)
    app._reconnect_apple_tv_if_needed("TV drawing power")
    app.states.update({LOCAL_TV: "on", REMOTE: "on"})
    return app


def provider_reports_on(app):
    app._on_hisense_reported_on(LOCAL_TV, "state", "unavailable", "on", {})


def source_changes(app, old, new):
    app.source = new
    app._on_tv_source_changed(LOCAL_TV, "source", old, new, {})


class AppleTvEarlyConnectWaitsForTheInput(unittest.TestCase):
    """The provider can report on before it knows the input, and a native app or a cast never
    gets one. No input is waited out; a named input decides at once."""

    def test_no_input_yet_waits_instead_of_disconnecting(self):
        app = early_connected_app()
        provider_reports_on(app)
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [60.0])
        self.assertTrue(app._apple_tv_input_unconfirmed)

    def test_the_apple_tv_input_arriving_inside_the_wait_keeps_the_connection(self):
        app = early_connected_app()
        provider_reports_on(app)
        source_changes(app, None, "HDMI2")
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
        fire(app, app._apple_tv_input_wait_due)
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertFalse(app._apple_tv_input_unconfirmed)
        self.assertIn("Apple TV early connect confirmed: TV reports HDMI2", app.logs)

    def test_no_input_for_the_whole_wait_disconnects_and_names_the_wait(self):
        app = early_connected_app()
        provider_reports_on(app)
        fire(app, app._apple_tv_input_wait_due)
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])
        self.assertFalse(app._apple_tv_input_unconfirmed)
        self.assertTrue(any("no input within the 60s wait" in line for line in app.logs), app.logs)

    def test_another_input_inside_the_wait_disconnects_at_once(self):
        for source in ("HDMI1", "Home"):
            with self.subTest(source=source):
                app = early_connected_app()
                provider_reports_on(app)
                source_changes(app, None, source)
                self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])
                self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
                fire(app, app._apple_tv_input_wait_due)
                self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])

    def test_a_first_report_naming_the_apple_tv_input_confirms_without_a_wait(self):
        app = early_connected_app()
        app.source = "HDMI2"
        provider_reports_on(app)
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertFalse(app._apple_tv_input_unconfirmed)
        self.assertIn("Apple TV early connect confirmed: TV reports HDMI2", app.logs)

    def test_a_first_report_naming_another_input_disconnects_without_a_wait(self):
        app = early_connected_app()
        app.source = "HDMI1"
        provider_reports_on(app)
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])

    def test_repeated_reports_of_no_input_keep_one_timer(self):
        app = early_connected_app()
        provider_reports_on(app)
        provider_reports_on(app)
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [60.0])

    def test_tv_off_during_the_wait_cancels_it_and_leaves_the_disconnect_to_the_grace(self):
        app = early_connected_app()
        provider_reports_on(app)
        tv_changes(app, "on", "off")
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
        fire(app, app._apple_tv_input_wait_due)
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertEqual(pending(app, app._apple_tv_disconnect_due), [120.0])

    def test_tv_back_on_after_a_cancelled_wait_waits_again_for_its_input(self):
        app = early_connected_app()
        provider_reports_on(app)
        tv_changes(app, "on", "off")
        tv_changes(app, "off", "on")
        provider_reports_on(app)
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [60.0])
        fire(app, app._apple_tv_input_wait_due)
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])

    def test_terminate_cancels_the_wait(self):
        app = early_connected_app()
        provider_reports_on(app)
        app.terminate()
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
        self.assertEqual(app.service_calls, [CONNECT])

    def test_any_disconnect_ends_the_wait(self):
        app = early_connected_app()
        provider_reports_on(app)
        app._disconnect_apple_tv("grace elapsed")
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
        self.assertFalse(app._apple_tv_input_unconfirmed)
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])

    def test_managed_selection_owning_the_switch_starts_no_wait(self):
        app = early_connected_app(**{SOURCE_SCRIPT: "on"})
        app.source_selection_script_entity = SOURCE_SCRIPT
        provider_reports_on(app)
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [])
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertFalse(app._apple_tv_input_unconfirmed)

    def test_managed_selection_starting_inside_the_wait_keeps_the_connection(self):
        app = early_connected_app(**{SOURCE_SCRIPT: "off"})
        app.source_selection_script_entity = SOURCE_SCRIPT
        provider_reports_on(app)
        app.states[SOURCE_SCRIPT] = "on"
        fire(app, app._apple_tv_input_wait_due)
        self.assertEqual(app.service_calls, [CONNECT])
        self.assertFalse(app._apple_tv_input_unconfirmed)

    def test_wait_length_follows_the_setting(self):
        app = early_connected_app()
        app.apple_tv_input_wait_seconds = 45.0
        provider_reports_on(app)
        self.assertEqual(pending(app, app._apple_tv_input_wait_due), [45.0])
        fire(app, app._apple_tv_input_wait_due)
        self.assertTrue(any("no input within the 45s wait" in line for line in app.logs), app.logs)

    def test_wait_comes_from_the_yaml_args(self):
        self.assertEqual(make_app(states(), {"apple_tv_input_wait_seconds": 45}).apple_tv_input_wait_seconds, 45.0)
        self.assertEqual(make_app(states()).apple_tv_input_wait_seconds, 60.0)
        config_path = Path(__file__).resolve().parents[1] / "living_room_tv_control.yaml"
        config = yaml.safe_load(config_path.read_text())["living_room_tv_control"]
        self.assertEqual(config["apple_tv_input_wait_seconds"], 60)
