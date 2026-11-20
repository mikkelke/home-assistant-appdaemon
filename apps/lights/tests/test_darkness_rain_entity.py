"""Darkness reads "is it raining" from binary_sensor.raining and from nothing else.

WHY THIS MATTERS: "raining" is one of the two gloomy triggers, and gloomy multiplies every
zone's DARK bar by ``gloomy_dark_multiplier`` (2.2) and doubles the dark->bright hold. For
family_room that moves the bar from 2500 lx to 5500 lx. The other trigger (overcast) already
covers a dim sky with the sun above 20 deg, so what a false "raining" costs is what happens
BELOW that - mornings, evenings, winter. The roof station's piezo contact
(``binary_sensor.gw2000a_rain_state_piezo``) is an impact detector, not a rain sensor: over the
60 days to 2026-08-12 it went ON 375 times for 142.1 h, and in 298 of those episodes the rate
never left 0.0 mm/h and the accumulator never moved. Believing it put 7.3 h of daylight across
14 days (worst single day 113 min, 2026-06-17) into DARK - lamps on in a room the sky was lighting
perfectly well - purely because something tapped the plate.

The rule that folds the contact and the measured rate into one honest answer lives in RainNow
(apps/weather/rain_now.py, tested in apps/weather/tests/test_rain_now.py). These tests pin what
darkness does with that answer: the entity on is rain and gloom, off is neither, and the
darkness-side "recent rain" grace and hold stretch work as they always did.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

import yaml

_LIGHTS_DIR = Path(__file__).resolve().parents[1]
if str(_LIGHTS_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTS_DIR))
sys.path.insert(0, str(_LIGHTS_DIR.parent / "presence"))

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

import darkness_calculator as dc  # noqa: E402

RAINING = "binary_sensor.raining"
CONTACT = "binary_sensor.gw2000a_rain_state_piezo"
RATE = "sensor.gw2000a_rain_rate_piezo"
OUTDOOR = "sensor.gw2000a_solar_lux"
INDOOR = "sensor.kitchen_presence_illuminance"

# The real family_room numbers from darkness_calculator.yaml - the zone the hours of phantom
# darkness landed in.
FAMILY_ROOM = {
    "family_room": {
        "sensors": [INDOOR],
        "outdoor_dark": 2500,
        "outdoor_bright": 8000,
        "indoor_min_bright": 280,
    }
}


def make_app(states, args=None, elevation=15.0):
    """DarknessCalculator through its REAL initialize() against a faked AD surface.

    Sun elevation defaults to 15 deg on purpose. Gloom has two triggers - rain and overcast
    (elevation >= gloomy_overcast_min_elevation=20 AND outdoor < 12000 lx) - and above 20 deg
    the overcast one would call these same dim-sky minutes gloomy regardless of rain. Below
    it, rain is the only trigger, so this is where a false rain does its own damage: at low
    sun, which is when the lamps matter."""
    app = dc.DarknessCalculator.__new__(dc.DarknessCalculator)
    base = {
        "zones": FAMILY_ROOM,
        "rain_sensor": RAINING,
        "outdoor_sensor": OUTDOOR,
        "gloomy_dark_multiplier": 2.2,
        "rain_hold_multiplier": 2.0,
        "hold_dark_to_bright_seconds": 1200,
    }
    base.update(args or {})
    app.args = base

    app._states = dict(states)
    app._attrs = {"sun.sun": {"elevation": elevation}}
    app.get_state = lambda entity, attribute=None, **kw: (
        app._attrs.get(entity, {}).get(attribute) if attribute is not None
        else app._states.get(entity)
    )
    app.listened = []
    app.listen_state = lambda cb, entity, **kw: app.listened.append((cb.__name__, entity))
    app._event_listeners = {}
    app.listen_event = lambda cb, event_name, **kw: app._event_listeners.__setitem__(event_name, cb)
    app.run_in = lambda cb, delay, **kw: None
    app.datetime = lambda: None
    app.run_every = lambda cb, start, interval: "periodic-handle"
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None
    app.log = lambda *a, **kw: None
    app.set_state_calls = []
    app.set_state = lambda entity, **kw: app.set_state_calls.append((entity, kw))
    app.initialize()
    return app


def states_with(raining=None, outdoor_lux=4000):
    states = {OUTDOOR: str(outdoor_lux), INDOOR: "600"}
    if raining is not None:
        states[RAINING] = raining
    return states


def rain_event(app, new):
    """The state change AppDaemon delivers when binary_sensor.raining flips."""
    app._debounced_recompute_all = lambda: None
    app._states[RAINING] = new
    app._on_rain(RAINING, None, None, new, {})


class TheEntityDecidesTheGloom(unittest.TestCase):
    def test_a_dry_sky_leaves_the_dark_bar_alone(self):
        """The whole point of the shared flag: no rain must not move family_room's DARK bar
        from 2500 to 5500 lx. At 4000 lx outdoors - inside that gap - the difference is lamps
        on or off."""
        app = make_app(states_with("off", outdoor_lux=4000))
        gloomy, why = app._gloomy()
        self.assertFalse(gloomy, f"a dry sky was called gloomy ({why})")
        target, reason = app._decide("family_room")
        self.assertNotEqual(target, dc.DARK, f"a dry sky forced DARK: {reason}")

    def test_rain_at_the_same_lux_does_force_dark(self):
        """Same 4000 lx sky, but with rain: gloomy applies, the bar goes to 5500 and the room
        is DARK. This is the behaviour the shared flag must NOT lose."""
        app = make_app(states_with("on", outdoor_lux=4000))
        gloomy, why = app._gloomy()
        self.assertTrue(gloomy)
        self.assertEqual(why, "rain")
        target, reason = app._decide("family_room")
        self.assertEqual(target, dc.DARK, reason)
        self.assertIn("gloomy: rain", reason)

    def test_high_sun_over_a_dim_sky_is_still_gloomy_without_any_rain(self):
        """Scope check, so nobody reads the shared flag as "rain no longer darkens anything".
        With the sun above gloomy_overcast_min_elevation (20 deg) and the sky under 12000 lx,
        the OVERCAST trigger fires on its own - the sky really is grey."""
        app = make_app(states_with("off", outdoor_lux=4000), elevation=30.0)
        gloomy, why = app._gloomy()
        self.assertTrue(gloomy)
        self.assertEqual(why, "overcast", "gloom must come from the sky, not from rain")

    def test_rain_stretches_the_dark_to_bright_hold(self):
        """rain_hold_multiplier doubles the 1200 s dark->bright hold while it rains."""
        app = make_app(states_with("off"))
        self.assertEqual(app._hold_needed(dc.DARK, dc.BRIGHT), 1200.0)
        rain_event(app, "on")
        self.assertEqual(app._hold_needed(dc.DARK, dc.BRIGHT), 2400.0)

    def test_the_published_zone_attributes_say_so(self):
        app = make_app(states_with("on"))
        attrs = app._zone_attrs("family_room", "test")
        self.assertTrue(attrs["raining"])
        self.assertTrue(attrs["rain_recent"])
        self.assertEqual(attrs["day_type"], "raining")
        rain_event(app, "off")
        attrs = app._zone_attrs("family_room", "test")
        self.assertFalse(attrs["raining"])
        self.assertTrue(attrs["rain_recent"], "the grace period outlives the rain")
        self.assertEqual(attrs["day_type"], "dry")


class RecentRainGraceIsDarknessOwn(unittest.TestCase):
    """rain_grace_seconds is a darkness decision (the sky stays grey after the last drop); the
    shared flag says when the rain stopped, darkness decides how long the sky stays wet."""

    def test_the_grace_period_follows_the_end_of_the_rain(self):
        app = make_app(states_with("on"))
        rain_event(app, "off")
        self.assertFalse(app._raining)
        self.assertTrue(app._rain_active_or_recent(), "the 30 min grace must still apply")
        gloomy, why = app._gloomy()
        self.assertEqual((gloomy, why), (True, "rain"))
        self.assertEqual(app._hold_needed(dc.DARK, dc.BRIGHT), 2400.0)

    def test_the_grace_period_runs_out(self):
        app = make_app(states_with("on"))
        rain_event(app, "off")
        app._rain_ended_at -= app.rain_grace_s + 1
        self.assertFalse(app._rain_active_or_recent())
        self.assertEqual(app._hold_needed(dc.DARK, dc.BRIGHT), 1200.0)

    def test_a_dry_start_has_no_grace(self):
        app = make_app(states_with("off"))
        self.assertFalse(app._rain_active_or_recent())


class SeedingEventsAndPeriodicReadTheSameEntity(unittest.TestCase):
    """All three entry points (init seed, event, periodic re-pull) must agree."""

    def test_init_seed_reads_the_entity(self):
        for state, raining in (("on", True), ("off", False), ("unavailable", False), ("unknown", False), (None, False)):
            with self.subTest(state=state):
                app = make_app(states_with(state))
                self.assertEqual(app._raining, raining)

    def test_the_event_sets_the_flag(self):
        app = make_app(states_with("off"))
        rain_event(app, "on")
        self.assertTrue(app._raining)
        rain_event(app, "off")
        self.assertFalse(app._raining)

    def test_an_entity_that_goes_unavailable_is_not_rain(self):
        app = make_app(states_with("on"))
        rain_event(app, "unavailable")
        self.assertFalse(app._raining)

    def test_periodic_repull_follows_the_entity(self):
        app = make_app(states_with("off"))
        app._states[RAINING] = "on"
        app._periodic()
        self.assertTrue(app._raining)
        app._states[RAINING] = "off"
        app._periodic()
        self.assertFalse(app._raining)

    def test_the_contact_and_the_rate_are_not_read_at_all(self):
        """Darkness must not see the piezo: a contact on over a measured rate, with the shared
        flag off, is a dry sky as far as the lights are concerned."""
        states = states_with("off")
        states[CONTACT] = "on"
        states[RATE] = "5.4"
        app = make_app(states)
        app._periodic()
        self.assertFalse(app._raining)
        self.assertEqual(app._gloomy()[0], False)

    def test_only_the_shared_entity_is_listened_to_for_rain(self):
        app = make_app(states_with("off"))
        entities = [entity for _, entity in app.listened]
        self.assertIn(RAINING, entities)
        self.assertNotIn(CONTACT, entities)
        self.assertNotIn(RATE, entities)
        self.assertEqual([cb for cb, entity in app.listened if entity == RAINING], ["_on_rain"])


class WithoutARainSensor(unittest.TestCase):
    def test_it_never_rains_and_nothing_listens(self):
        app = make_app(states_with("on"), args={"rain_sensor": None})
        self.assertFalse(app._raining)
        app._periodic()
        self.assertFalse(app._raining)
        self.assertNotIn(RAINING, [entity for _, entity in app.listened])


class TheRealYamlWiresTheSharedEntity(unittest.TestCase):
    def test_initialize_with_the_real_config_listens_to_binary_sensor_raining(self):
        config = yaml.safe_load((_LIGHTS_DIR / "darkness_calculator.yaml").read_text())["darkness_calculator"]
        app = make_app({OUTDOOR: "4000", RAINING: "on"}, args={k: v for k, v in config.items() if k not in ("module", "class", "log")})
        self.assertEqual(app.rain_sensor, "binary_sensor.raining")
        self.assertTrue(app._raining)
        self.assertEqual([entity for _, entity in app.listened].count(RAINING), 1)


if __name__ == "__main__":
    unittest.main()
