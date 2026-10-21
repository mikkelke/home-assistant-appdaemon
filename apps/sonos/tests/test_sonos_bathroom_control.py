"""SonosBathroomControl: the bath presence boost.

Someone in the bath raises the bathroom speaker by bath_presence_volume_boost (up to
presence_on_max_volume) and leaving takes the same amount back. While the wake-up window is open
(WakeupRoutine.wake_volume_cap answers with a level) the boost only brings the bathroom up to its
standard volume - it never lifts it above, never lowers it - and leaving takes nothing back, so
the morning does not end with a bathroom quieter than it started. Outside the window, and when
the wake routine cannot be asked, nothing changes. The wall buttons are the person's own hand and
are never limited.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
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

import sonos_bathroom_control as sbc  # noqa: E402

BATHROOM = "media_player.bathroom"
SENSOR = "binary_sensor.bathroom_bath_presence_presence"


def wake_app(cap):
    """get_app() for a wake routine whose wake_volume_cap answers `cap` (None: no window)."""
    asked = []

    def wake_volume_cap(entity):
        asked.append(entity)
        return cap

    def get_app(name):
        return SimpleNamespace(wake_volume_cap=wake_volume_cap) if name == "WakeupBedroom" else None

    get_app.asked = asked
    return get_app


def make_app(volume=0.18, *, get_app=None, present=False, max_volume=0.60):
    """SonosBathroomControl with fake AppDaemon callables, without running initialize()."""
    app = sbc.SonosBathroomControl.__new__(sbc.SonosBathroomControl)
    app.media_player = BATHROOM
    app.bath_presence_volume_boost = 0.1
    app.presence_on_max_volume = max_volume
    app.volume_step = 0.05
    app.zwave_property_key_volume_up = "004"
    app.zwave_property_key_volume_down = "003"
    app._logically_present = present
    app._last_presence_change_time = 0.0
    app._presence_throttle_seconds = 0.5
    app.wake_app = "WakeupBedroom"
    app._presence_boost_held = False
    app.volume = volume
    app.writes = []

    def call_service(service, **kw):
        if service == "media_player/volume_set":
            app.volume = kw["volume_level"]
            app.writes.append(kw["volume_level"])

    app.call_service = call_service
    app.get_state = lambda entity, **kw: app.volume
    app.get_app = get_app or (lambda name: None)
    app.log = MagicMock()
    app.error = MagicMock()
    return app


def person_arrives(app):
    app._last_presence_change_time = 0.0
    app._handle_presence_change(SENSOR, None, "off", "on", {})


def person_leaves(app):
    app._last_presence_change_time = 0.0
    app._handle_presence_change(SENSOR, None, "on", "off", {})


class OutsideTheWakeWindowNothingChanged(unittest.TestCase):
    def test_arriving_raises_the_bathroom_and_leaving_takes_it_back(self):
        app = make_app(0.18, get_app=wake_app(None))
        person_arrives(app)
        self.assertEqual(app.volume, 0.28)
        person_leaves(app)
        self.assertEqual(app.volume, 0.18)

    def test_the_presence_maximum_still_applies(self):
        app = make_app(0.55, get_app=wake_app(None))
        person_arrives(app)
        self.assertEqual(app.volume, 0.60)
        app = make_app(0.60, get_app=wake_app(None))
        person_arrives(app)
        self.assertEqual(app.writes, [])

    def test_a_wake_routine_that_cannot_be_asked_changes_nothing(self):
        def broken(name):
            raise RuntimeError("app registry unavailable")

        for get_app in (None, broken, lambda name: SimpleNamespace(), lambda name: SimpleNamespace(
                wake_volume_cap=lambda entity: 1 / 0)):
            app = make_app(0.18, get_app=get_app)
            person_arrives(app)
            self.assertEqual(app.volume, 0.28)
            person_leaves(app)
            self.assertEqual(app.volume, 0.18)

    def test_leaving_after_a_boost_given_outside_the_window_takes_it_back_inside_it(self):
        app = make_app(0.18, get_app=wake_app(None))
        person_arrives(app)
        app.get_app = wake_app(0.18)
        person_leaves(app)
        self.assertEqual(app.volume, 0.18)


class WhileTheWakeWindowIsOpen(unittest.TestCase):
    def test_a_bathroom_at_its_standard_is_not_raised_and_nothing_is_taken_back(self):
        app = make_app(0.18, get_app=wake_app(0.18))
        person_arrives(app)
        person_leaves(app)
        self.assertEqual(app.writes, [])
        self.assertEqual(app.volume, 0.18)

    def test_a_bathroom_below_its_standard_is_raised_but_never_past_it(self):
        for before, after in ((0.06, 0.16), (0.09, 0.18), (0.12, 0.18), (0.17, 0.18)):
            app = make_app(before, get_app=wake_app(0.18))
            person_arrives(app)
            self.assertEqual(app.volume, after, before)

    def test_leaving_after_a_held_boost_takes_nothing_back(self):
        app = make_app(0.06, get_app=wake_app(0.18))
        person_arrives(app)
        person_leaves(app)
        self.assertEqual(app.volume, 0.16)

    def test_a_bathroom_already_above_its_standard_is_never_lowered(self):
        app = make_app(0.24, get_app=wake_app(0.18))
        person_arrives(app)
        person_leaves(app)
        self.assertEqual(app.writes, [])
        self.assertEqual(app.volume, 0.24)

    def test_leaving_after_the_window_has_closed_still_takes_nothing_back(self):
        app = make_app(0.18, get_app=wake_app(0.18))
        person_arrives(app)
        app.get_app = wake_app(None)
        person_leaves(app)
        self.assertEqual(app.volume, 0.18)

    def test_the_next_visit_after_the_window_boosts_as_always(self):
        app = make_app(0.18, get_app=wake_app(0.18))
        person_arrives(app)
        person_leaves(app)
        app.get_app = wake_app(None)
        person_arrives(app)
        self.assertEqual(app.volume, 0.28)
        person_leaves(app)
        self.assertEqual(app.volume, 0.18)

    def test_the_log_says_the_window_is_limiting_the_boost(self):
        app = make_app(0.18, get_app=wake_app(0.18))
        person_arrives(app)
        person_leaves(app)
        logged = [str(c) for c in app.log.call_args_list]
        self.assertTrue(any("wake window open" in line and "0.18" in line for line in logged))
        self.assertTrue(any("nothing to take back" in line for line in logged))

    def test_the_wake_routine_is_asked_about_this_speaker(self):
        get_app = wake_app(0.18)
        person_arrives(make_app(0.18, get_app=get_app))
        self.assertEqual(get_app.asked, [BATHROOM])

    def test_the_wall_buttons_are_never_limited(self):
        app = make_app(0.18, get_app=wake_app(0.18))
        app.handle_volume_event("zwave_js_value_notification", {"property_key": "004"}, {})
        self.assertEqual(app.volume, 0.23)


class TheWakeAppNameIsConfigurable(unittest.TestCase):
    def initialized(self, overrides=None):
        app = sbc.SonosBathroomControl.__new__(sbc.SonosBathroomControl)
        app.args = {"media_player": BATHROOM, "device_id": "dev", "volume_step": 0.05,
                    "bath_presence_sensor": SENSOR, "bath_presence_volume_boost": 0.1,
                    "presence_on_max_volume": 0.6, **(overrides or {})}
        app.log = MagicMock()
        app.error = MagicMock()
        app.listen_event = MagicMock()
        app.listen_state = MagicMock()
        app.get_state = lambda entity, **kw: "off"
        app.initialize()
        return app

    def test_defaults_to_the_wake_routine(self):
        self.assertEqual(self.initialized().wake_app, "WakeupBedroom")
        self.assertFalse(self.initialized()._presence_boost_held)

    def test_can_be_pointed_elsewhere(self):
        self.assertEqual(self.initialized({"wake_app": "OtherWake"}).wake_app, "OtherWake")


if __name__ == "__main__":
    unittest.main()
