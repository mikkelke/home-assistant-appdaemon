from __future__ import annotations

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


def make_app(states, reset_enabled=True, refresh_in_progress=False):
    """BedroomTVControl with fake get_state/call_service/run_in, without
    running AppDaemon's initialize()."""
    app = btc.BedroomTVControl.__new__(btc.BedroomTVControl)
    app.tv_entity = "media_player.bedroom_tv"
    app.sony_tv_entity = "media_player.bedroom_sony_tv"
    app.apple_tv_entity = "media_player.bedroom_apple_tv"
    app.apple_tv_reset_enabled = reset_enabled
    app._apple_refresh_in_progress = refresh_in_progress
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


if __name__ == "__main__":
    unittest.main()
