from __future__ import annotations

import datetime as _dt
import sys
import types
import unittest
from pathlib import Path

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

import bedroom_tv_control as btc  # noqa: E402


POWER_SENSOR = "sensor.bedroom_tv_relay_power"


def make_app(states, reset_enabled=True, refresh_in_progress=False, power_sensor=None):
    """BedroomTVControl with fake get_state/call_service/run_in, without
    running AppDaemon's initialize(). Pass power_sensor=POWER_SENSOR (and a
    watts string under that key in `states`) to exercise the power-meter
    arbitration; the default None mirrors a meterless config."""
    app = btc.BedroomTVControl.__new__(btc.BedroomTVControl)
    app.tv_entity = "media_player.bedroom_tv"
    app.sony_tv_entity = "media_player.bedroom_sony_tv"
    app.apple_tv_entity = "media_player.bedroom_apple_tv"
    app.apple_tv_reset_enabled = reset_enabled
    app._apple_refresh_in_progress = refresh_in_progress
    app.power_sensor = power_sensor
    app.power_on_watts = 25.0
    app.power_off_confirm_seconds = 3.0
    app._power_fired_at = None
    app._get_lift_position = lambda: "Down"
    app._pending_raise_handle = None
    app._blip_guard_until = None
    app.blip_guard_hours = 4.0
    app.lift_command_timeout_s = 4.0
    app._lift_action_in_progress = False
    app._lift_action_start_time = None
    app._last_lift_command_time = None
    app._reset_lift_action_flag = lambda: None
    app._start_periodic_verification = lambda: None
    app.datetime = lambda: _dt.datetime(2026, 7, 27, 12, 0, 0)
    app.get_state = lambda entity, **kw: states.get(entity)
    app.log = lambda *a, **kw: None
    app.service_calls = []
    app.scheduled = []
    app.call_service = lambda service, **kw: app.service_calls.append((service, kw))

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    app.run_in = run_in
    return app


def scheduled_callbacks(app):
    return [cb for cb, _delay, _kw in app.scheduled]


class StartupStaleAppleTvHeal(unittest.TestCase):
    """2026-07-16: a deploy stopped the apps for ~1 min and the TV was turned off
    inside the gap. The OFF-path heal never saw the transition, so on restart
    _check_initial_tv_state read the stale-paused Apple TV as "TV on" and held the
    lift DOWN all day. The startup check must now apply the same cure as
    _raise_lift_if_still_off: sleep the Apple TV, then re-check and raise."""

    def test_stale_paused_apple_tv_is_slept_and_recheck_scheduled(self):
        app = make_app({
            "media_player.bedroom_tv": "off",
            "media_player.bedroom_sony_tv": "off",
            "media_player.bedroom_apple_tv": "paused",
        })
        app._check_initial_tv_state()
        self.assertIn(
            ("remote/turn_off", {"entity_id": "remote.bedroom_apple_tv"}),
            app.service_calls,
        )
        self.assertIn(app._raise_after_apple_refresh, scheduled_callbacks(app))
        self.assertTrue(app._apple_refresh_in_progress)
        self.assertNotIn(app._ensure_lift_down_if_tv_active, scheduled_callbacks(app))

    def test_sony_standby_also_counts_as_off_for_the_heal(self):
        app = make_app({
            "media_player.bedroom_tv": "off",
            "media_player.bedroom_sony_tv": "standby",
            "media_player.bedroom_apple_tv": "paused",
        })
        app._check_initial_tv_state()
        self.assertIn(
            ("remote/turn_off", {"entity_id": "remote.bedroom_apple_tv"}),
            app.service_calls,
        )
        self.assertIn(app._raise_after_apple_refresh, scheduled_callbacks(app))

    def test_playing_apple_tv_is_never_slept(self):
        # 2026-06-23 rule: genuine playback must never be slept/refreshed/raised.
        app = make_app({
            "media_player.bedroom_tv": "off",
            "media_player.bedroom_sony_tv": "off",
            "media_player.bedroom_apple_tv": "playing",
        })
        app._check_initial_tv_state()
        self.assertEqual(app.service_calls, [])
        self.assertFalse(app._apple_refresh_in_progress)
        self.assertIn(app._ensure_lift_down_if_tv_active, scheduled_callbacks(app))

    def test_no_heal_when_reset_disabled(self):
        app = make_app({
            "media_player.bedroom_tv": "off",
            "media_player.bedroom_sony_tv": "off",
            "media_player.bedroom_apple_tv": "paused",
        }, reset_enabled=False)
        app._check_initial_tv_state()
        self.assertEqual(app.service_calls, [])
        self.assertIn(app._ensure_lift_down_if_tv_active, scheduled_callbacks(app))

    def test_no_double_heal_when_refresh_already_in_progress(self):
        app = make_app({
            "media_player.bedroom_tv": "off",
            "media_player.bedroom_sony_tv": "off",
            "media_player.bedroom_apple_tv": "paused",
        }, refresh_in_progress=True)
        app._check_initial_tv_state()
        self.assertEqual(app.service_calls, [])
        self.assertIn(app._ensure_lift_down_if_tv_active, scheduled_callbacks(app))

    def test_sony_on_means_someone_is_watching_lift_stays_down(self):
        app = make_app({
            "media_player.bedroom_tv": "on",
            "media_player.bedroom_sony_tv": "on",
            "media_player.bedroom_apple_tv": "idle",
        })
        app._check_initial_tv_state()
        self.assertEqual(app.service_calls, [])
        self.assertFalse(app._apple_refresh_in_progress)
        self.assertIn(app._ensure_lift_down_if_tv_active, scheduled_callbacks(app))

    def test_genuinely_off_tv_still_takes_the_plain_raise_path(self):
        app = make_app({
            "media_player.bedroom_tv": "off",
            "media_player.bedroom_sony_tv": "off",
            "media_player.bedroom_apple_tv": "off",
        })
        app._check_initial_tv_state()
        self.assertEqual(app.service_calls, [])
        self.assertIn(app._raise_lift_after_stop, scheduled_callbacks(app))
        self.assertNotIn(app._raise_after_apple_refresh, scheduled_callbacks(app))


class DarkPanelArbitration(unittest.TestCase):
    """2026-07-27: the Shelly PM Mini proved every state source can lie 'on'
    over a dark panel (Bravia post-off rebound at the debounce check; AD 4.5.13
    serving a stale store 'on' even to a freshly restarted app, unhealable by
    update_entity because HA-side state was already off = no event). A panel
    drawing <= power_on_watts cannot be lit (idle floor ~2.5W, warm post-off
    ~17W vs >=110W lit, >=30W booting), so power arbitrates: kick + bounded
    re-checks, then the raise proceeds anyway."""

    STALE_ON = {
        "media_player.bedroom_tv": "on",
        "media_player.bedroom_sony_tv": "on",
        "media_player.bedroom_apple_tv": "off",
        POWER_SENSOR: "15.7",
    }

    def test_first_dark_check_kicks_integration_and_reschedules(self):
        app = make_app(dict(self.STALE_ON), power_sensor=POWER_SENSOR)
        app._raise_lift_if_still_off({"path_marker": "tv_off"})
        self.assertIn(
            ("homeassistant/update_entity", {"entity_id": "media_player.bedroom_sony_tv"}),
            app.service_calls,
        )
        recheck = [(cb, kw) for cb, _d, kw in app.scheduled if cb == app._raise_lift_if_still_off]
        self.assertEqual(len(recheck), 1)
        self.assertEqual(recheck[0][1].get("dark_panel_attempts"), 1)
        # no raise yet: the stop-then-raise sequence must not have started
        self.assertNotIn(
            ("script/turn_on", {"entity_id": "script.bedroom_tv_lift_stop"}),
            app.service_calls,
        )

    def test_exhausted_dark_checks_mean_power_wins_and_lift_raises(self):
        app = make_app(dict(self.STALE_ON), power_sensor=POWER_SENSOR)
        app._raise_lift_if_still_off({"path_marker": "dark_panel", "dark_panel_attempts": 3})
        self.assertIn(
            ("script/turn_on", {"entity_id": "script.bedroom_tv_lift_stop"}),
            app.service_calls,
        )
        self.assertIn(app._raise_lift_after_stop, scheduled_callbacks(app))

    def test_playing_apple_tv_blocks_dark_arbitration(self):
        # 2026-06-23 rule holds even against the meter: never raise over playback.
        states = dict(self.STALE_ON, **{"media_player.bedroom_apple_tv": "playing"})
        app = make_app(states, power_sensor=POWER_SENSOR)
        app._raise_lift_if_still_off({"path_marker": "tv_off", "dark_panel_attempts": 3})
        self.assertEqual(app.service_calls, [])
        self.assertNotIn(app._raise_lift_after_stop, scheduled_callbacks(app))

    def test_lit_panel_states_on_is_a_genuine_skip(self):
        states = dict(self.STALE_ON, **{POWER_SENSOR: "130.0"})
        app = make_app(states, power_sensor=POWER_SENSOR)
        app._raise_lift_if_still_off({"path_marker": "tv_off"})
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.scheduled, [])

    def test_meterless_config_keeps_the_old_skip_behavior(self):
        states = {k: v for k, v in self.STALE_ON.items() if k != POWER_SENSOR}
        app = make_app(states)
        app._raise_lift_if_still_off({"path_marker": "tv_off"})
        self.assertEqual(app.service_calls, [])
        self.assertEqual(app.scheduled, [])

    def test_dark_panel_lets_apple_heal_run_despite_stale_sony_on(self):
        # Compound lie: AD-stale 'on' for universal+sony while the Apple TV is
        # genuinely paused and the panel is dark -> the paused-abandoned cure
        # (sleep + re-check) applies, not a force-raise over paused content.
        states = dict(self.STALE_ON, **{"media_player.bedroom_apple_tv": "paused"})
        app = make_app(states, power_sensor=POWER_SENSOR)
        app._raise_lift_if_still_off({"path_marker": "tv_off"})
        self.assertIn(
            ("remote/turn_off", {"entity_id": "remote.bedroom_apple_tv"}),
            app.service_calls,
        )
        self.assertIn(app._raise_after_apple_refresh, scheduled_callbacks(app))


class PowerVetoRetries(unittest.TestCase):
    """A real off with lingering draw must not strand the lift: the veto now
    re-checks (bounded) instead of bare-returning. Measured decay is
    off -> ~17W within 1s, so in practice retries fire only on true blips."""

    GLOWING = {
        "media_player.bedroom_tv": "off",
        "media_player.bedroom_sony_tv": "off",
        "media_player.bedroom_apple_tv": "off",
        POWER_SENSOR: "45.0",
    }

    def test_veto_reschedules_and_arms_blip_guard(self):
        app = make_app(dict(self.GLOWING), power_sensor=POWER_SENSOR)
        app._raise_lift_if_still_off({"path_marker": "tv_off"})
        self.assertIsNotNone(app._blip_guard_until)
        recheck = [(cb, kw) for cb, _d, kw in app.scheduled if cb == app._raise_lift_if_still_off]
        self.assertEqual(len(recheck), 1)
        self.assertEqual(recheck[0][1].get("power_veto_retries"), 1)
        self.assertNotIn(
            ("script/turn_on", {"entity_id": "script.bedroom_tv_lift_stop"}),
            app.service_calls,
        )

    def test_veto_gives_up_after_three_retries(self):
        app = make_app(dict(self.GLOWING), power_sensor=POWER_SENSOR)
        app._raise_lift_if_still_off({"path_marker": "power_veto_retry", "power_veto_retries": 3})
        self.assertEqual(app.scheduled, [])
        self.assertNotIn(
            ("script/turn_on", {"entity_id": "script.bedroom_tv_lift_stop"}),
            app.service_calls,
        )


class StartupDarkPanelArbitration(unittest.TestCase):
    """2026-07-27, 16:51: a freshly restarted app read AD's stale 'on' for
    Sony + universal and re-lowered the lift over a 15.7W panel. The startup
    check must route a state-claims-on/panel-dark contradiction to the OFF-path
    arbitration instead of trusting the claim."""

    def test_stale_on_with_dark_panel_arbitrates_instead_of_lowering(self):
        app = make_app({
            "media_player.bedroom_tv": "on",
            "media_player.bedroom_sony_tv": "on",
            "media_player.bedroom_apple_tv": "off",
            POWER_SENSOR: "15.7",
        }, power_sensor=POWER_SENSOR)
        app._check_initial_tv_state()
        self.assertIn(app._raise_lift_if_still_off, scheduled_callbacks(app))
        self.assertNotIn(app._ensure_lift_down_if_tv_active, scheduled_callbacks(app))

    def test_lit_panel_keeps_the_normal_startup_lower(self):
        app = make_app({
            "media_player.bedroom_tv": "on",
            "media_player.bedroom_sony_tv": "on",
            "media_player.bedroom_apple_tv": "off",
            POWER_SENSOR: "130.0",
        }, power_sensor=POWER_SENSOR)
        app._check_initial_tv_state()
        self.assertIn(app._ensure_lift_down_if_tv_active, scheduled_callbacks(app))
        self.assertNotIn(app._raise_lift_if_still_off, scheduled_callbacks(app))


class PowerFallBelt(unittest.TestCase):
    """Falling watts is the belt for a LOST off event (2026-07-27 16:45: AD's
    store dropped the off; no state event ever reached the app and the lift
    stayed pinned Down). A >threshold -> <=threshold edge schedules the same
    raise check the state event would have - unless one is already pending."""

    def test_falling_edge_schedules_raise_check(self):
        app = make_app({}, power_sensor=POWER_SENSOR)
        app._on_tv_power(POWER_SENSOR, "state", "131.0", "17.7", {})
        self.assertIn(app._raise_lift_if_still_off, scheduled_callbacks(app))

    def test_no_duplicate_when_state_event_already_scheduled(self):
        app = make_app({}, power_sensor=POWER_SENSOR)
        app._pending_raise_handle = object()
        app._on_tv_power(POWER_SENSOR, "state", "131.0", "17.7", {})
        self.assertEqual(app.scheduled, [])

    def test_standby_noise_is_not_an_edge(self):
        app = make_app({}, power_sensor=POWER_SENSOR)
        app._on_tv_power(POWER_SENSOR, "state", "17.7", "2.5", {})
        self.assertEqual(app.scheduled, [])

    def test_stowed_lift_needs_no_rescue(self):
        app = make_app({}, power_sensor=POWER_SENSOR)
        app._get_lift_position = lambda: "Up"
        app._on_tv_power(POWER_SENSOR, "state", "131.0", "17.7", {})
        self.assertEqual(app.scheduled, [])


class EnsureDownDarkGuard(unittest.TestCase):
    """Periodic verification / ensure-down must never hold a lift DOWN over a
    dark panel - that is how the 07-27 stale store pinned the lift all
    afternoon. Deliberate ON transitions bypass (boot draw lags ~1s)."""

    def test_dark_panel_blocks_a_stateless_ensure_down(self):
        app = make_app({
            "media_player.bedroom_tv": "on",
            "media_player.bedroom_sony_tv": "on",
            "media_player.bedroom_apple_tv": "off",
            POWER_SENSOR: "2.5",
        }, power_sensor=POWER_SENSOR)
        app._get_lift_position = lambda: self.fail("dark guard must return before touching the lift")
        app._ensure_lift_down_if_tv_active({})
        self.assertEqual(app.service_calls, [])


if __name__ == "__main__":
    unittest.main()
