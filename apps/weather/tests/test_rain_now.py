"""RainNow: binary_sensor.raining, the one "is it raining now" of the house.

WHY THIS MATTERS: ``binary_sensor.gw2000a_rain_state_piezo`` is not a rain sensor. The WS90's
piezo plate reports an impact - a fly, a leaf, a bird, the mount shaking - and Home Assistant
publishes that as a rain binary. Measured against the recorder over the 60 days to 2026-08-12:
the contact went ON 375 times for 142.1 h total, and in 298 of those episodes the rain RATE
never left 0.0 mm/h and the rain accumulator never moved a single 0.1 mm tick. Zero water. 60 of
those 298 onsets happened under more than 200 W/m2 of sunshine, and their median relative
humidity was 77% with a 4.1 K dew-point spread, so they were not dew or fog either.

Everything that reacts to rain (the lights' gloomy sky, the terrace-door alert that announces
over Sonos and pushes every phone, the room displays, the dashboard sky) used to decide for
itself, so they could disagree. They now read this one entity.

The rate sensor is the instrument that measures water: it quantises in 0.6 mm/h steps and every
non-zero reading in those 60 days was corroborated by the accumulator. It also catches rain the
contact misses - 12.6% of measurable-rate samples arrived while the contact read "off", including
22.2 mm/h on 2026-06-27 23:33.

So the rule these tests pin is: measured rate wins outright; the contact may only EXTEND an
episode the rate has already confirmed (real showers dip to 0.0 mm/h mid-episode, and the sky
stays wet after the last tick - median 2 min, p90 60 min); an unconfirmed contact is ignored.

Replaying the rule over those 60 days of recorded piezo history turns 142.1 h of contact-on into
44.4 h of rain: all 77 real episodes survive, 99.6 h of phantom is dropped, and four short
stretches of rate-only rain the contact never reported are gained.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

import yaml

_WEATHER_DIR = Path(__file__).resolve().parents[1]
APPS = _WEATHER_DIR.parent
if str(_WEATHER_DIR) not in sys.path:
    sys.path.insert(0, str(_WEATHER_DIR))

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

import rain_now as rn  # noqa: E402

CONFIG = yaml.safe_load((_WEATHER_DIR / "rain_now.yaml").read_text())["RainNow"]
ENTITY = CONFIG["entity"]
CONTACT = CONFIG["contact_entity"]
RATE = CONFIG["rate_entity"]


def judge(contact_on, rate, confirmed=False):
    return rn.apply_rain(contact_on, rate, confirmed)


class PhantomContactIsNotRain(unittest.TestCase):
    """The 298 zero-millimetre episodes must not read as rain."""

    def test_contact_on_with_zero_rate_is_not_raining(self):
        """2026-07-05 21:31Z: the contact stayed on for 440 minutes with the rate flat at 0.0
        mm/h and not one accumulator tick. Eight hours of "rain" that never fell."""
        raining, confirmed, reason = judge(True, 0.0)
        self.assertFalse(raining)
        self.assertFalse(confirmed)
        self.assertEqual(reason, "piezo tap without measured rain, ignored")

    def test_a_phantom_stays_a_phantom_however_long_it_lasts(self):
        confirmed = False
        for _ in range(440):
            raining, confirmed, _ = judge(True, 0.0, confirmed)
            self.assertFalse(raining)

    def test_dry_is_dry(self):
        self.assertEqual(judge(False, 0.0), (False, False, "dry"))


class RateWinsOutright(unittest.TestCase):
    """The rate sensor is the instrument that measures water - trust it in both directions."""

    def test_measurable_rate_is_rain_even_with_the_contact_off(self):
        """12.6% of measurable-rate samples arrived while the contact read 'off' - the worst
        being 22.2 mm/h on 2026-06-27 23:33. Torrential rain the contact never noticed."""
        self.assertEqual(judge(False, 22.2), (True, True, "measured 22.2 mm/h"))

    def test_smallest_reportable_rate_counts(self):
        """The piezo quantises in 0.6 mm/h steps, so 0.6 is the smallest non-zero reading that
        exists. The 0.5 bar must therefore mean 'any measurable rain', not 'moderate'."""
        self.assertTrue(judge(True, 0.6)[0])
        self.assertTrue(judge(False, 0.6)[0])
        self.assertLessEqual(CONFIG["rate_min_mmh"], 0.6)

    def test_the_bar_is_inclusive_and_below_it_is_not_rain(self):
        self.assertTrue(judge(False, 0.5)[0])
        self.assertFalse(judge(False, 0.4)[0])

    def test_unreadable_rate_cannot_confirm(self):
        """Station offline: the contact can stick 'on' stale through the outage (seen
        2026-07-08). With no rate to corroborate it, that is not rain."""
        self.assertEqual(judge(True, None), (False, False, "rain rate unreadable"))
        self.assertEqual(judge(False, None), (False, False, "rain rate unreadable"))

    def test_the_bar_comes_from_the_caller(self):
        self.assertTrue(rn.apply_rain(False, 0.3, False, rate_min=0.3)[0])
        self.assertFalse(rn.apply_rain(False, 0.3, False, rate_min=0.4)[0])


class ConfirmedEpisodesSurviveTheirGaps(unittest.TestCase):
    """A real shower's rate dips to 0.0 mid-episode; the sky stays wet and grey."""

    def test_contact_holds_a_confirmed_episode_through_a_zero_rate_dip(self):
        """2026-08-05 03:33Z: 225 min of continuous contact, of which 92 min read below the bar
        between bursts that peaked at 32.4 mm/h. One episode, not several."""
        _, confirmed, _ = judge(True, 3.6)                       # burst
        raining, confirmed, reason = judge(True, 0.0, confirmed)  # lull, contact still closed
        self.assertTrue(raining, "a confirmed episode ended at the first dry minute")
        self.assertEqual(reason, "piezo still wet after measured rain")

    def test_the_episode_ends_when_the_contact_opens(self):
        """After the last measurable tick the contact stays closed a median 2 min (p90 60 min)
        - that tail is the wet sky. Once it opens, the episode is over."""
        _, confirmed, _ = judge(True, 3.6)
        self.assertEqual(judge(False, 0.0, confirmed), (False, False, "dry"))

    def test_a_new_contact_episode_needs_its_own_confirmation(self):
        """Confirmation must not leak across episodes: yesterday's shower cannot license
        today's fly. This is what makes the 298 phantoms stay phantoms."""
        _, confirmed, _ = judge(True, 3.6)
        _, confirmed, _ = judge(False, 0.0, confirmed)   # episode over, confirmation cleared
        raining, _, _ = judge(True, 0.0, confirmed)      # new contact episode, no water
        self.assertFalse(raining)


def make_app(states=None, **overrides):
    """RainNow through its REAL initialize() with the real yaml, against a faked AD surface in
    which set_state writes into the same state dict get_state reads, as HA does."""
    app = rn.RainNow.__new__(rn.RainNow)
    app.args = {**CONFIG, **overrides}
    app._states = dict(states or {})
    app.get_state = lambda entity, attribute=None, **kw: (
        None if attribute is not None else app._states.get(entity)
    )
    app.listened = []
    app.listen_state = lambda cb, entity, **kw: app.listened.append((cb.__name__, entity))
    app.events = []
    app.listen_event = lambda cb, event_name, **kw: app.events.append(event_name)
    app.scheduled = []
    app.run_every = lambda cb, start, interval, **kw: app.scheduled.append((cb.__name__, start, interval))
    app.published = []
    app.publish_options = []
    app.fail_publish = False

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        if app.fail_publish:
            raise RuntimeError("HA unreachable")
        app.published.append((entity, state, dict(attributes), replace))
        app.publish_options.append(kw)
        app._states[entity] = state

    app.set_state = set_state
    app.logged = []
    app.log = lambda msg, *a, **kw: app.logged.append((msg, kw.get("level")))
    app.initialize()
    return app


def dry():
    return {CONTACT: "off", RATE: "0.0"}


def set_sources(app, contact=None, rate=None):
    """Change a source in the fake HA and deliver the event the way AppDaemon would."""
    if contact is not None:
        app._states[CONTACT] = contact
        app._on_source(CONTACT, None, None, contact, {})
    if rate is not None:
        app._states[RATE] = rate
        app._on_source(RATE, None, None, rate, {})


def now_state(app):
    return app._states[ENTITY]


def last_attributes(app):
    return app.published[-1][2]


class PublishedEntity(unittest.TestCase):
    def test_a_dry_start_publishes_off_with_its_evidence(self):
        app = make_app(dry())
        self.assertEqual(len(app.published), 1)
        entity, state, attributes, replace = app.published[0]
        self.assertEqual((entity, state, replace), ("binary_sensor.raining", "off", True))
        self.assertEqual(
            attributes,
            {
                "friendly_name": "Raining",
                "device_class": "moisture",
                "icon": "mdi:weather-rainy",
                "reason": "dry",
                "rate_mmh": 0.0,
                "piezo": "off",
            },
        )

    def test_the_entity_is_new_after_every_ha_restart_so_no_existence_check(self):
        app = make_app(dry())
        self.assertEqual(app.publish_options, [{"check_existence": False}])

    def test_a_measured_start_publishes_on(self):
        app = make_app({CONTACT: "off", RATE: "1.2"})
        self.assertEqual(now_state(app), "on")
        self.assertEqual(last_attributes(app)["reason"], "measured 1.2 mm/h")
        self.assertEqual(last_attributes(app)["rate_mmh"], 1.2)

    def test_a_cold_start_does_not_believe_an_unconfirmed_contact(self):
        """After a restart nothing has confirmed the contact episode yet, so a contact that is
        on while the rate reads 0.0 is not rain."""
        app = make_app({CONTACT: "on", RATE: "0.0"})
        self.assertEqual(now_state(app), "off")
        self.assertEqual(last_attributes(app)["reason"], "piezo tap without measured rain, ignored")
        self.assertEqual(last_attributes(app)["piezo"], "on")

    def test_a_cold_start_believes_a_contact_with_water(self):
        app = make_app({CONTACT: "on", RATE: "2.4"})
        self.assertEqual(now_state(app), "on")

    def test_missing_or_unavailable_sources_are_not_rain(self):
        for states in ({}, {CONTACT: "unavailable", RATE: "unavailable"}, {CONTACT: "unknown", RATE: "unknown"}):
            with self.subTest(states=states):
                app = make_app(states)
                self.assertEqual(now_state(app), "off")
                self.assertEqual(last_attributes(app)["reason"], "rain rate unreadable")
                self.assertIsNone(last_attributes(app)["rate_mmh"])

    def test_both_sources_are_listened_to_and_a_tick_is_scheduled(self):
        app = make_app(dry())
        self.assertEqual(sorted(entity for _, entity in app.listened), sorted([CONTACT, RATE]))
        self.assertEqual(app.scheduled, [("_tick", "now+60", 60)])

    def test_the_entity_and_sources_come_from_the_yaml(self):
        app = make_app(
            {"binary_sensor.den_piezo": "off", "sensor.den_rate": "3.0"},
            entity="binary_sensor.den_raining",
            contact_entity="binary_sensor.den_piezo",
            rate_entity="sensor.den_rate",
            rate_min_mmh=3.0,
        )
        self.assertEqual(app.published[0][0], "binary_sensor.den_raining")
        self.assertEqual(app.published[0][1], "on")
        self.assertEqual(sorted(entity for _, entity in app.listened), ["binary_sensor.den_piezo", "sensor.den_rate"])


class EpisodesThroughEvents(unittest.TestCase):
    """The same rule on every entry point: init seed, source events and the periodic re-pull."""

    def test_a_rate_event_alone_starts_an_episode(self):
        """The rate listener exists precisely because the contact misses real rain."""
        app = make_app(dry())
        set_sources(app, rate="4.8")
        self.assertEqual(now_state(app), "on")
        self.assertEqual(last_attributes(app)["reason"], "measured 4.8 mm/h")

    def test_a_phantom_contact_event_changes_the_evidence_but_not_the_answer(self):
        app = make_app(dry())
        set_sources(app, contact="on")
        self.assertEqual(now_state(app), "off")
        self.assertEqual(last_attributes(app)["piezo"], "on")

    def test_a_whole_shower(self):
        app = make_app(dry())
        set_sources(app, contact="on")                 # a tap on the plate
        self.assertEqual(now_state(app), "off")
        set_sources(app, contact="off")
        set_sources(app, contact="on", rate="3.6")     # the shower starts
        self.assertEqual(now_state(app), "on")
        set_sources(app, rate="0.0")                   # a lull, contact still closed
        self.assertEqual(now_state(app), "on")
        self.assertEqual(last_attributes(app)["reason"], "piezo still wet after measured rain")
        set_sources(app, contact="off")                # the contact opens: over
        self.assertEqual(now_state(app), "off")
        set_sources(app, contact="on")                 # another tap, no water
        self.assertEqual(now_state(app), "off")

    def test_the_periodic_repull_applies_the_same_rule(self):
        app = make_app(dry())
        app._states[CONTACT] = "on"
        app._tick({})
        self.assertEqual(now_state(app), "off")
        app._states[RATE] = "1.2"
        app._tick({})
        self.assertEqual(now_state(app), "on")

    def test_a_changing_rate_inside_rain_updates_the_attributes_only(self):
        app = make_app({CONTACT: "off", RATE: "0.6"})
        set_sources(app, rate="2.4")
        self.assertEqual(now_state(app), "on")
        self.assertEqual(last_attributes(app)["rate_mmh"], 2.4)
        self.assertEqual(last_attributes(app)["reason"], "measured 2.4 mm/h")

    def test_only_a_change_of_the_answer_is_logged(self):
        app = make_app(dry())
        before = len(app.logged)
        set_sources(app, rate="0.6")
        set_sources(app, rate="1.2")
        set_sources(app, rate="0.0")
        raining_lines = [m for m, _ in app.logged[before:]]
        self.assertEqual(len(raining_lines), 2)
        self.assertTrue(raining_lines[0].startswith("Raining"))
        self.assertTrue(raining_lines[1].startswith("Not raining"))


class RestartSurvival(unittest.TestCase):
    """An HA restart erases the set_state entity. AppDaemon re-creates the app around it, so a
    fresh initialize() must write the entity even when HA still has it; a vanished entity with
    no app restart is healed by the next tick."""

    def test_a_fresh_init_publishes_even_when_ha_still_has_the_entity(self):
        app = make_app({**dry(), ENTITY: "off"})
        self.assertEqual(len(app.published), 1)

    def test_a_tick_changes_nothing_while_ha_has_the_entity_and_nothing_moved(self):
        app = make_app(dry())
        app._tick({})
        app._tick({})
        self.assertEqual(len(app.published), 1)

    def test_a_tick_rewrites_an_entity_ha_lost(self):
        app = make_app({CONTACT: "off", RATE: "1.2"})
        del app._states[ENTITY]
        app._tick({})
        self.assertEqual(len(app.published), 2)
        self.assertEqual(now_state(app), "on")
        self.assertEqual(app.published[1][2], app.published[0][2])

    def test_a_failed_publish_is_retried_by_the_next_tick(self):
        app = make_app(dry())
        app.fail_publish = True
        set_sources(app, rate="1.2")
        self.assertEqual(now_state(app), "off")
        self.assertIn("WARNING", [level for _, level in app.logged])
        app.fail_publish = False
        app._tick({})
        self.assertEqual(now_state(app), "on")

    def test_no_reconnect_event_listener_is_registered(self):
        """AD terminates every app while the plugin is down, so a listener for the reconnect
        event could never receive it."""
        self.assertEqual(make_app(dry()).events, [])


class EveryConsumerReadsTheSharedFlag(unittest.TestCase):
    """So that no two places can disagree about the rain, the other apps carry no rain sensor
    of their own: they name this entity and nothing else."""

    @staticmethod
    def load(relative):
        return yaml.safe_load((APPS / relative).read_text())

    def test_consumers_name_the_published_entity(self):
        self.assertEqual(self.load("lights/darkness_calculator.yaml")["darkness_calculator"]["rain_sensor"], ENTITY)
        self.assertEqual(self.load("weather/weather_opening_alert.yaml")["WeatherOpeningAlert"]["rain_entity"], ENTITY)
        displays = self.load("home_pulse/bathroom_display.yaml")
        self.assertEqual(displays["BathroomDisplay"]["rain_entity"], ENTITY)
        self.assertEqual(displays["BedroomDisplay"]["rain_entity"], ENTITY)

    def test_consumers_carry_no_rate_sensor_or_threshold(self):
        for relative, app in (("lights/darkness_calculator.yaml", "darkness_calculator"),
                              ("weather/weather_opening_alert.yaml", "WeatherOpeningAlert")):
            with self.subTest(app=app):
                keys = set(self.load(relative)[app])
                self.assertEqual({key for key in keys if "rain_rate" in key or "rate_min" in key}, set())

    def test_nothing_but_this_app_reads_the_piezo_contact(self):
        readers = sorted(
            str(path.relative_to(APPS))
            for path in APPS.rglob("*")
            if path.suffix in (".py", ".yaml")
            and "tests" not in path.parts
            and CONTACT in path.read_text()
        )
        self.assertEqual(readers, ["weather/rain_now.py", "weather/rain_now.yaml"])


if __name__ == "__main__":
    unittest.main()
