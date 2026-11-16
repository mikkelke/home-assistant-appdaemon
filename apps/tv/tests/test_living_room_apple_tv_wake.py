import unittest

from test_living_room_apple_tv_disconnect import (
    APPLE,
    HISENSE,
    REMOTE,
    TV,
    make_app,
    states,
    tv_changes,
)

SOURCE_SCRIPT = "script.living_room_tv_select_source"
WAKE = ("media_player/turn_on", {"entity_id": APPLE})
CONNECT = ("remote/turn_on", {"entity_id": REMOTE})
DISCONNECT = ("remote/turn_off", {"entity_id": REMOTE})


def wake_app(source="HDMI2", source_gate="HDMI2", script=True, **overrides):
    """TV on `source` with the Apple TV integration connected to a sleeping box (remote on,
    player off)."""
    args = {"apple_tv_source_name": source_gate, "apple_tv_reset_enabled": False}
    if script:
        args["source_selection_script_entity"] = SOURCE_SCRIPT
    app = make_app(states(**{TV: "on", HISENSE: "on", REMOTE: "on", APPLE: "off", **overrides}), args)
    app.source = source
    app.logs = []
    app.log = lambda message, **kwargs: app.logs.append(message)

    def get_state(entity, attribute=None, **kwargs):
        if entity == HISENSE and attribute == "source":
            return app.source
        return app.states.get(entity)

    app.get_state = get_state
    return app


def source_changes(app, old, new):
    app.source = new
    app._on_tv_source_changed(HISENSE, "source", old, new, {})


def remote_connects(app):
    app.states[REMOTE] = "on"
    app._on_apple_tv_remote_connected(REMOTE, None, "off", "on", {})


def early_connected_app(source=None):
    """TV drawing power while the provider cannot be reached: the integration connects early
    and the TV has not named its input yet."""
    app = wake_app(source=source, **{TV: "unavailable", HISENSE: "unavailable", REMOTE: "off"})
    app._tv_relay_power_suggests_tv_on = lambda: True
    app._reconnect_apple_tv_if_needed("TV drawing power")
    app.states.update({HISENSE: "on", REMOTE: "on"})
    return app


class WakeConditions(unittest.TestCase):
    """The TV's HDMI-CEC does not wake the box, so a TV on its input shows no signal.
    media_player.turn_on wakes it, but only while the integration is connected, which is
    remote on; a connected sleeping box reads player off."""

    def test_tv_on_the_apple_tv_input_with_a_connected_sleeping_box_wakes_it(self):
        app = wake_app()
        app._wake_apple_tv_if_asleep("test")
        self.assertEqual(app.service_calls, [WAKE])
        self.assertIn(
            "Apple TV wake (test): TV on HDMI2 and the box is asleep - "
            "media_player.turn_on sent to media_player.living_room_apple_tv",
            app.logs,
        )

    def test_a_fresh_app_has_not_woken_the_box(self):
        self.assertFalse(wake_app()._apple_tv_woken)

    def test_another_input_does_not_wake_it(self):
        for source in ("HDMI1", "HDMI3", "Home", None, "unknown", "unavailable", ""):
            with self.subTest(source=source):
                app = wake_app(source=source)
                app._wake_apple_tv_if_asleep("test")
                self.assertEqual(app.service_calls, [])
                self.assertFalse(app._apple_tv_woken)

    def test_a_tv_that_does_not_report_on_does_not_wake_it(self):
        for primary in ("off", "standby", "unavailable", "unknown", None):
            with self.subTest(primary=primary):
                app = wake_app(**{HISENSE: primary})
                app._wake_apple_tv_if_asleep("test")
                self.assertEqual(app.service_calls, [])

    def test_a_player_that_is_not_off_does_not_get_woken(self):
        for player in ("idle", "playing", "paused", "standby", "on", "unavailable", "unknown", None):
            with self.subTest(player=player):
                app = wake_app(**{APPLE: player})
                app._wake_apple_tv_if_asleep("test")
                self.assertEqual(app.service_calls, [])

    def test_a_disconnected_integration_is_not_woken_because_the_call_would_do_nothing(self):
        for remote in ("off", "unavailable", "unknown", None):
            with self.subTest(remote=remote):
                app = wake_app(**{REMOTE: remote})
                app._wake_apple_tv_if_asleep("test")
                self.assertEqual(app.service_calls, [])
                self.assertFalse(app._apple_tv_woken)

    def test_the_selection_script_owns_the_waking_while_it_runs(self):
        app = wake_app(**{SOURCE_SCRIPT: "on"})
        app._wake_apple_tv_if_asleep("test")
        self.assertEqual(app.service_calls, [])
        self.assertFalse(app._apple_tv_woken)
        app.states[SOURCE_SCRIPT] = "off"
        app._wake_apple_tv_if_asleep("test")
        self.assertEqual(app.service_calls, [WAKE])

    def test_the_selection_script_is_optional(self):
        app = wake_app(script=False)
        self.assertIsNone(app.source_selection_script_entity)
        app._wake_apple_tv_if_asleep("test")
        self.assertEqual(app.service_calls, [WAKE])

    def test_without_the_input_gate_nothing_is_woken(self):
        app = wake_app(source_gate=None)
        app._wake_apple_tv_if_asleep("test")
        self.assertEqual(app.service_calls, [])
        remote_connects(app)
        source_changes(app, "HDMI1", "HDMI2")
        self.assertEqual(app.service_calls, [])

    def test_the_gate_follows_the_configured_input_name(self):
        app = wake_app(source="HDMI3", source_gate="HDMI3")
        app._wake_apple_tv_if_asleep("test")
        self.assertEqual(app.service_calls, [WAKE])

    def test_unreadable_states_wake_nothing(self):
        app = wake_app()

        def unavailable(entity, **kwargs):
            raise RuntimeError("ha unreachable")

        app.get_state = unavailable
        app._wake_apple_tv_if_asleep("test")
        self.assertEqual(app.service_calls, [])
        self.assertTrue(any("Apple TV wake (test) failed" in line for line in app.logs), app.logs)

    def test_a_failed_call_is_swallowed_and_leaves_the_session_unused(self):
        app = wake_app()

        def unavailable(service, **kwargs):
            raise RuntimeError("ha unreachable")

        app.call_service = unavailable
        app._wake_apple_tv_if_asleep("test")
        self.assertFalse(app._apple_tv_woken)
        app.call_service = lambda service, **kwargs: app.service_calls.append((service, kwargs))
        app._wake_apple_tv_if_asleep("retry")
        self.assertEqual(app.service_calls, [WAKE])


class OncePerTvSession(unittest.TestCase):
    """The integration also reconnects by itself when the box was put to sleep on purpose, so
    one wake per TV session; an explicit switch to the input or a TV that went off starts a
    new one."""

    def test_a_second_trigger_in_the_same_session_does_not_wake_again(self):
        app = wake_app()
        app._wake_apple_tv_if_asleep("first")
        app._wake_apple_tv_if_asleep("second")
        remote_connects(app)
        app._on_hisense_reported_on(HISENSE, "state", "off", "on", {})
        self.assertEqual(app.service_calls, [WAKE])
        self.assertTrue(app._apple_tv_woken)

    def test_switching_to_the_input_again_wakes_again(self):
        app = wake_app()
        app._wake_apple_tv_if_asleep("first")
        source_changes(app, "HDMI1", "HDMI2")
        self.assertEqual(app.service_calls, [WAKE, WAKE])

    def test_switching_away_and_back_wakes_again(self):
        app = wake_app(**{REMOTE: "off"})
        source_changes(app, "HDMI1", "HDMI2")
        remote_connects(app)
        remote_connects(app)
        self.assertEqual(app.service_calls, [CONNECT, WAKE])
        source_changes(app, "HDMI2", "HDMI1")
        app.states[REMOTE] = "off"
        source_changes(app, "HDMI1", "HDMI2")
        remote_connects(app)
        self.assertEqual(app.service_calls, [CONNECT, WAKE, DISCONNECT, CONNECT, WAKE])

    def test_leaving_the_input_does_not_wake_anything(self):
        app = wake_app()
        source_changes(app, "HDMI2", "HDMI1")
        self.assertNotIn(WAKE, app.service_calls)

    def test_the_tv_going_off_starts_a_new_session(self):
        app = wake_app()
        app._wake_apple_tv_if_asleep("first")
        app.states[HISENSE] = "off"
        tv_changes(app, "on", "off")
        self.assertFalse(app._apple_tv_woken)
        app.states[HISENSE] = "on"
        tv_changes(app, "off", "on")
        app._on_hisense_reported_on(HISENSE, "state", "off", "on", {})
        self.assertEqual(app.service_calls, [WAKE, WAKE])

    def test_other_tv_state_changes_do_not_start_a_new_session(self):
        app = wake_app()
        app._wake_apple_tv_if_asleep("first")
        tv_changes(app, "on", "playing")
        tv_changes(app, "playing", "on")
        app._wake_apple_tv_if_asleep("second")
        self.assertEqual(app.service_calls, [WAKE])

    def test_a_tv_that_is_already_off_and_stays_off_changes_nothing(self):
        app = wake_app()
        app._wake_apple_tv_if_asleep("first")
        tv_changes(app, "off", "off")
        app._wake_apple_tv_if_asleep("second")
        self.assertEqual(app.service_calls, [WAKE])


class WakeTriggers(unittest.TestCase):
    def test_the_integration_connecting_with_the_tv_on_the_input_wakes_the_box(self):
        app = wake_app(**{REMOTE: "off"})
        remote_connects(app)
        self.assertEqual(app.service_calls, [WAKE])
        self.assertIn(
            "Apple TV wake (Apple TV integration connected): TV on HDMI2 and the box is asleep - "
            "media_player.turn_on sent to media_player.living_room_apple_tv",
            app.logs,
        )

    def test_the_integration_connecting_with_the_tv_on_another_input_wakes_nothing(self):
        app = wake_app(source="HDMI1")
        remote_connects(app)
        self.assertEqual(app.service_calls, [])

    def test_the_integration_connecting_with_the_tv_off_wakes_nothing(self):
        app = wake_app(**{TV: "off", HISENSE: "off"})
        remote_connects(app)
        self.assertEqual(app.service_calls, [])

    def test_a_connection_cut_after_the_grace_is_not_followed_by_a_wake(self):
        app = wake_app(**{TV: "off", HISENSE: "off"})
        app._apple_tv_grace_elapsed = True
        remote_connects(app)
        self.assertEqual(app.service_calls, [DISCONNECT])

    def test_a_connection_that_arrives_while_the_selection_script_runs_is_not_woken(self):
        app = wake_app(**{SOURCE_SCRIPT: "on"})
        remote_connects(app)
        self.assertEqual(app.service_calls, [])

    def test_switching_to_the_input_while_the_box_is_connected_wakes_it(self):
        app = wake_app()
        source_changes(app, "HDMI1", "HDMI2")
        self.assertEqual(app.service_calls, [WAKE])

    def test_switching_to_the_input_while_disconnected_reconnects_and_wakes_once_connected(self):
        app = wake_app(**{REMOTE: "off"})
        source_changes(app, "HDMI1", "HDMI2")
        self.assertEqual(app.service_calls, [CONNECT])
        remote_connects(app)
        self.assertEqual(app.service_calls, [CONNECT, WAKE])

    def test_switching_to_the_input_with_the_tv_not_on_wakes_nothing(self):
        for primary in ("off", "unavailable", "unknown", None):
            with self.subTest(primary=primary):
                app = wake_app(**{HISENSE: primary})
                source_changes(app, "HDMI1", "HDMI2")
                self.assertEqual(app.service_calls, [])

    def test_switching_to_another_input_wakes_nothing(self):
        for new in ("HDMI1", "Home", None):
            with self.subTest(new=new):
                app = wake_app(source=new)
                source_changes(app, "HDMI2", new)
                self.assertNotIn(WAKE, app.service_calls)

    def test_the_tv_reporting_on_with_the_input_selected_wakes_the_box(self):
        app = wake_app()
        app._on_hisense_reported_on(HISENSE, "state", "off", "on", {})
        self.assertEqual(app.service_calls, [WAKE])

    def test_the_tv_reporting_on_with_another_input_wakes_nothing(self):
        app = wake_app(source="HDMI1")
        app._on_hisense_reported_on(HISENSE, "state", "off", "on", {})
        self.assertEqual(app.service_calls, [])

    def test_the_input_confirming_an_early_connect_wakes_the_box(self):
        app = early_connected_app()
        app._on_hisense_reported_on(HISENSE, "state", "unavailable", "on", {})
        self.assertEqual(app.service_calls, [CONNECT])
        source_changes(app, None, "HDMI2")
        self.assertEqual(app.service_calls, [CONNECT, WAKE])
        self.assertIn("Apple TV early connect confirmed: TV reports HDMI2", app.logs)
        self.assertTrue(any(line.startswith("Apple TV wake (early connect confirmed)") for line in app.logs), app.logs)

    def test_a_first_report_naming_the_input_wakes_once(self):
        app = early_connected_app(source="HDMI2")
        app._on_hisense_reported_on(HISENSE, "state", "unavailable", "on", {})
        self.assertEqual(app.service_calls, [CONNECT, WAKE])

    def test_an_early_connect_that_finds_another_input_is_cut_and_not_woken(self):
        app = early_connected_app()
        app._on_hisense_reported_on(HISENSE, "state", "unavailable", "on", {})
        source_changes(app, None, "HDMI1")
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])

    def test_a_switch_the_early_connect_wait_consumes_starts_a_new_session(self):
        app = early_connected_app()
        app._apple_tv_woken = True
        app._on_hisense_reported_on(HISENSE, "state", "unavailable", "on", {})
        source_changes(app, None, "HDMI2")
        self.assertEqual(app.service_calls, [CONNECT, WAKE])

    def test_the_early_connect_wait_running_out_wakes_nothing(self):
        app = early_connected_app()
        app._on_hisense_reported_on(HISENSE, "state", "unavailable", "on", {})
        app._apple_tv_input_wait_due({})
        self.assertEqual(app.service_calls, [CONNECT, DISCONNECT])


if __name__ == "__main__":
    unittest.main()
