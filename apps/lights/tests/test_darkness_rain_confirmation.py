"""The piezo rain CONTACT may not darken the apartment on its own (2026-08-12).

WHY THIS MATTERS: ``binary_sensor.gw2000a_rain_state_piezo`` is not a rain sensor. The
WS90's piezo plate reports an impact - a fly, a leaf, a bird, the mount shaking - and Home
Assistant publishes that as a rain binary. Measured against the recorder over the 60 days to
2026-08-12: the contact went ON 375 times for 142.1 h total, and in 298 of those episodes the
rain RATE never left 0.0 mm/h and the rain accumulator never moved a single 0.1 mm tick. Zero
water. 60 of those 298 onsets happened under more than 200 W/m2 of sunshine, and their median
relative humidity was 77% with a 4.1 K dew-point spread, so they were not dew or fog either.

Inside darkness_calculator "raining" is one of the two gloomy triggers, and gloomy multiplies
every zone's DARK bar by ``gloomy_dark_multiplier`` (2.2) and doubles the dark->bright hold.
For family_room that moves the bar from 2500 lx to 5500 lx. Gloom's other trigger (overcast)
already covers a dim sky with the sun above 20 deg, so the phantom's own contribution is what
happens BELOW that - mornings, evenings, winter. Over the same 60 days it put 7.3 h of
daylight across 14 days (worst single day 113 min, 2026-06-17) into DARK - lamps on in a room
the sky was lighting perfectly well - purely because something tapped the plate.

The rate sensor is the instrument that measures water: it quantises in 0.6 mm/h steps and
every non-zero reading in those 60 days was corroborated by the accumulator. It also catches
rain the contact misses - 12.6% of measurable-rate samples arrived while the contact read
"off", including 22.2 mm/h on 2026-06-27 23:33.

So the rule these tests pin is: measured rate wins outright; the contact may only EXTEND an
episode the rate has already confirmed (real showers dip to 0.0 mm/h mid-episode, and the sky
stays wet after the last tick - median 2 min, p90 60 min); an unconfirmed contact is ignored.

Replaying the shipped _apply_rain over those 60 days of recorded piezo history turns 142.1 h
of contact-on into 44.4 h of rain: all 77 real episodes survive, 99.6 h of phantom is dropped,
and four short stretches of rate-only rain the contact never reported are gained.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

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

import darkness_calculator as dc  # noqa: E402

CONTACT = "binary_sensor.gw2000a_rain_state_piezo"
RATE = "sensor.gw2000a_rain_rate_piezo"
OUTDOOR = "sensor.gw2000a_solar_lux"
INDOOR = "sensor.kitchen_presence_illuminance"

# The real family_room numbers from darkness_calculator.yaml - the zone the 8.9 h of
# phantom darkness landed in.
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
    it, rain is the only trigger, so this is where the phantom contact does its own damage:
    all 7.3 h of it, at low sun, which is when the lamps matter."""
    app = dc.DarknessCalculator.__new__(dc.DarknessCalculator)
    base = {
        "zones": FAMILY_ROOM,
        "rain_sensor": CONTACT,
        "rain_rate_sensor": RATE,
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
    app.listen_state = lambda *a, **kw: None
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


def dry_states(outdoor_lux=4000):
    return {
        CONTACT: "off",
        RATE: "0.0",
        OUTDOOR: str(outdoor_lux),
        INDOOR: "600",
    }


class PhantomContactIsNotRain(unittest.TestCase):
    """The 298 zero-millimetre episodes must not read as rain."""

    def test_contact_on_with_zero_rate_is_not_raining(self):
        """2026-07-05 21:31Z: the contact stayed on for 440 minutes with the rate flat at
        0.0 mm/h and not one accumulator tick. Eight hours of "rain" that never fell."""
        app = make_app(dry_states())
        app._apply_rain(True, 0.0)
        self.assertFalse(app._raining)
        self.assertTrue(app._rain_contact, "the raw contact is still reported as-is")

    def test_phantom_contact_leaves_the_dark_bar_alone(self):
        """The whole point: a phantom must not move family_room's DARK bar from 2500 to
        5500 lx. At 4000 lx outdoors - inside that gap - the difference is lamps on or off."""
        app = make_app(dry_states(outdoor_lux=4000))
        app._apply_rain(True, 0.0)
        gloomy, why = app._gloomy()
        self.assertFalse(gloomy, f"phantom contact made the sky 'gloomy' ({why})")
        target, reason = app._decide("family_room")
        self.assertNotEqual(target, dc.DARK, f"phantom rain forced DARK: {reason}")

    def test_a_confirmed_shower_at_the_same_lux_does_force_dark(self):
        """Same 4000 lx sky, but with water actually falling: gloomy applies, the bar goes
        to 5500 and the room is DARK. This is the behaviour the fix must NOT lose."""
        app = make_app(dry_states(outdoor_lux=4000))
        app._apply_rain(True, 1.2)
        gloomy, why = app._gloomy()
        self.assertTrue(gloomy)
        self.assertEqual(why, "rain")
        target, reason = app._decide("family_room")
        self.assertEqual(target, dc.DARK, reason)
        self.assertIn("gloomy: rain", reason)

    def test_high_sun_over_a_dim_sky_is_still_gloomy_without_any_rain(self):
        """Scope check, so nobody reads this fix as "rain no longer darkens anything". With
        the sun above gloomy_overcast_min_elevation (20 deg) and the sky under 12000 lx, the
        OVERCAST trigger fires on its own - the sky really is grey. That is why only the
        7.3 h below 20 deg were ever attributable to the phantom contact."""
        app = make_app(dry_states(outdoor_lux=4000), elevation=30.0)
        app._apply_rain(True, 0.0)
        gloomy, why = app._gloomy()
        self.assertTrue(gloomy)
        self.assertEqual(why, "overcast", "gloom must come from the sky, not the phantom")

    def test_phantom_does_not_stretch_the_dark_to_bright_hold(self):
        """rain_hold_multiplier doubles the 1200 s dark->bright hold. 184 h of phantom
        cover (episodes + the 30 min grace) over 60 days was holding lamps on that long
        after the room had gone bright."""
        app = make_app(dry_states())
        app._apply_rain(True, 0.0)
        self.assertEqual(app._hold_needed(dc.DARK, dc.BRIGHT), 1200.0)
        app._apply_rain(True, 0.6)
        self.assertEqual(app._hold_needed(dc.DARK, dc.BRIGHT), 2400.0)


class RateWinsOutright(unittest.TestCase):
    """The rate sensor is the instrument that measures water - trust it in both directions."""

    def test_measurable_rate_is_rain_even_with_the_contact_off(self):
        """12.6% of measurable-rate samples arrived while the contact read 'off' - the worst
        being 22.2 mm/h on 2026-06-27 23:33. Torrential rain the contact never noticed."""
        app = make_app(dry_states())
        app._apply_rain(False, 22.2)
        self.assertTrue(app._raining)

    def test_smallest_reportable_rate_counts(self):
        """The piezo quantises in 0.6 mm/h steps, so 0.6 is the smallest non-zero reading
        that exists. The 0.5 bar must therefore mean 'any measurable rain', not 'moderate'."""
        app = make_app(dry_states())
        app._apply_rain(True, 0.6)
        self.assertTrue(app._raining)

    def test_unreadable_rate_cannot_confirm(self):
        """Station offline: the contact can stick 'on' stale through the outage (seen
        2026-07-08). With no rate to corroborate it, that is not rain."""
        app = make_app(dry_states())
        app._apply_rain(True, None)
        self.assertFalse(app._raining)


class ConfirmedEpisodesSurviveTheirGaps(unittest.TestCase):
    """A real shower's rate dips to 0.0 mid-episode; the sky stays wet and grey."""

    def test_contact_holds_a_confirmed_episode_through_a_zero_rate_dip(self):
        """2026-08-05 03:33Z: 225 min of continuous contact, of which 92 min read below the
        bar between bursts that peaked at 32.4 mm/h. One episode, not several."""
        app = make_app(dry_states())
        app._apply_rain(True, 3.6)      # burst
        app._apply_rain(True, 0.0)      # lull, contact still closed
        self.assertTrue(app._raining, "a confirmed episode ended at the first dry minute")

    def test_the_episode_ends_when_the_contact_opens(self):
        """After the last measurable tick the contact stays closed a median 2 min (p90
        60 min) - that tail is the wet sky. Once it opens, the episode is over and
        rain_grace_seconds takes it from there."""
        app = make_app(dry_states())
        app._apply_rain(True, 3.6)
        app._apply_rain(False, 0.0)
        self.assertFalse(app._raining)
        self.assertTrue(app._rain_active_or_recent(), "the 30 min grace must still apply")

    def test_a_new_contact_episode_needs_its_own_confirmation(self):
        """Confirmation must not leak across episodes: yesterday's shower cannot license
        today's fly. This is what makes the 298 phantoms stay phantoms."""
        app = make_app(dry_states())
        app._apply_rain(True, 3.6)
        app._apply_rain(False, 0.0)     # episode over, confirmation cleared
        app._apply_rain(True, 0.0)      # new contact episode, no water
        self.assertFalse(app._raining)


class LegacyBehaviourWithoutARateSensor(unittest.TestCase):
    """Leaving rain_rate_sensor unset must keep the old contact-only semantics, so the
    knob can be backed out without editing code."""

    def test_contact_alone_still_means_rain_when_no_rate_sensor_configured(self):
        app = make_app(dry_states(), args={"rain_rate_sensor": None})
        app._apply_rain(True, None)
        self.assertTrue(app._raining)

    def test_no_rate_listener_is_registered_without_the_sensor(self):
        seen = []
        app = dc.DarknessCalculator.__new__(dc.DarknessCalculator)
        app.args = {"zones": FAMILY_ROOM, "rain_sensor": CONTACT, "outdoor_sensor": OUTDOOR}
        app._states = dry_states()
        app._attrs = {"sun.sun": {"elevation": 30.0}}
        app.get_state = lambda entity, attribute=None, **kw: (
            app._attrs.get(entity, {}).get(attribute) if attribute is not None
            else app._states.get(entity)
        )
        app.listen_state = lambda cb, entity, **kw: seen.append(entity)
        app.listen_event = lambda *a, **kw: None
        app.run_in = lambda cb, delay, **kw: None
        app.datetime = lambda: None
        app.run_every = lambda cb, start, interval: "h"
        app.timer_running = lambda handle: False
        app.cancel_timer = lambda handle: None
        app.log = lambda *a, **kw: None
        app.set_state = lambda entity, **kw: None
        app.initialize()
        self.assertIn(CONTACT, seen)
        self.assertNotIn(RATE, seen)


class SeedingAndPeriodicUseTheSameRule(unittest.TestCase):
    """All three entry points (init seed, event, periodic re-pull) must agree - a phantom
    that only the periodic path believes is just as expensive as one the event path does."""

    def test_init_seed_does_not_believe_an_unconfirmed_contact(self):
        states = dry_states()
        states[CONTACT] = "on"
        app = make_app(states)
        self.assertFalse(app._raining)
        self.assertTrue(app._rain_contact)

    def test_init_seed_believes_a_contact_with_water(self):
        states = dry_states()
        states[CONTACT] = "on"
        states[RATE] = "2.4"
        app = make_app(states)
        self.assertTrue(app._raining)

    def test_periodic_repull_applies_the_same_confirmation(self):
        app = make_app(dry_states())
        app._states[CONTACT] = "on"
        app._periodic()
        self.assertFalse(app._raining)
        app._states[RATE] = "1.2"
        app._periodic()
        self.assertTrue(app._raining)

    def test_rate_event_alone_starts_an_episode(self):
        """The rate listener exists precisely because the contact misses real rain."""
        app = make_app(dry_states())
        app._debounced_recompute_all = lambda: None
        app._on_rain_rate(RATE, None, "0.0", "4.8", {})
        self.assertTrue(app._raining)


if __name__ == "__main__":
    unittest.main()
