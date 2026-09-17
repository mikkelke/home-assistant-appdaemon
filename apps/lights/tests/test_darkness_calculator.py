"""Unit tests for darkness_calculator's restart-survival publish/self-heal machinery
(2026-07-27). Not a test of the dark/bright decision rules themselves (those already
have informal coverage via room_state_darkness's tests) - a single ``always_dark`` zone
is used throughout so ``_decide`` is trivially deterministic (always DARK) and the tests
can focus purely on the publish/cache-heal paths."""

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

ZONES = {"testzone": {"always_dark": True}}
BIN_ENT = "binary_sensor.dark_testzone"
SEN_ENT = "sensor.darkness_testzone"
ROOM_ENT = "sensor.room_state_testzone"


def make_app(states=None, attrs=None, args=None):
    """DarknessCalculator built via the REAL initialize() against a fully faked AD
    surface (listen_state/listen_event/run_every/run_in are no-ops - nothing is
    auto-invoked, so tests call _recompute_all/_periodic/_spotcheck_republish
    directly for full control)."""
    app = dc.DarknessCalculator.__new__(dc.DarknessCalculator)
    app.args = dict(args if args is not None else {"zones": ZONES})

    app._states = dict(states or {})
    app._attrs = dict(attrs or {})
    app.get_state = lambda entity, attribute=None, **kw: (
        app._attrs.get(entity, {}).get(attribute) if attribute is not None else app._states.get(entity)
    )

    app.listen_state = lambda *a, **kw: None
    app._event_listeners = {}
    app.listen_event = lambda cb, event_name, **kw: app._event_listeners.__setitem__(event_name, cb)
    app.run_in = lambda cb, delay, **kw: None
    app.datetime = lambda: None
    app.run_every = lambda cb, start, interval: "periodic-handle"
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None

    app.log_calls = []
    app.log = lambda msg, *a, **kw: app.log_calls.append((msg, kw))
    app.set_state_calls = []
    app.set_state = lambda entity, **kw: app.set_state_calls.append((entity, kw))

    app.initialize()
    return app


def calls_for(app, entity):
    return [c for c in app.set_state_calls if c[0] == entity]


class PostRestartRepublish(unittest.TestCase):
    """An HA restart tears this app down and re-creates it: AD 4.5.13 terminates every app
    in the namespace when the HASS plugin drops and only restarts them after the reconnect.
    So the heal for restart-wiped entities is initialize()'s own recompute against an empty
    snapshot cache. A "plugin_started" listener cannot serve that role - AD fires that event
    while the apps are still terminated - hence no reconnect-event listener may be added."""

    def test_no_reconnect_event_listener_is_registered(self):
        app = make_app()
        self.assertEqual(app._event_listeners, {})

    def test_fresh_init_republishes_every_entity_even_when_ha_still_has_them(self):
        # Entities present in HA (i.e. nothing environmental will look "changed" either):
        # the first recompute after initialize() must still write all three, because the
        # snapshot cache a restart starts with is empty.
        app = make_app(states={BIN_ENT: "on", SEN_ENT: "dark", ROOM_ENT: "Empty (Dark)"})
        app._recompute_all()  # the run_in(..., 2) that initialize() schedules
        for ent in (BIN_ENT, SEN_ENT, ROOM_ENT):
            self.assertEqual(len(calls_for(app, ent)), 1, ent)


class SpotcheckRepublish(unittest.TestCase):
    """Belt-and-braces self-heal: each periodic tick spot-checks one published entity in
    rotation and, if HA doesn't have it, clears the whole cache and republishes."""

    def test_noop_when_the_spot_checked_entity_is_present(self):
        app = make_app()
        app._recompute_all()
        count_before = len(calls_for(app, BIN_ENT))

        names = list(app._publish_snapshots.keys())
        app._spotcheck_idx = names.index(BIN_ENT)
        app._states[BIN_ENT] = "on"  # HA has it - no self-heal needed

        app._spotcheck_republish()

        self.assertEqual(len(calls_for(app, BIN_ENT)), count_before)

    def test_self_heals_when_ha_is_missing_the_spot_checked_entity(self):
        app = make_app()
        app._recompute_all()
        count_before = len(calls_for(app, BIN_ENT))

        # HA doesn't have BIN_ENT (restart-wiped) even though the cache says published -
        # rotate the index directly onto it so the test is deterministic regardless of
        # dict key ordering. (app._states never had it to begin with, same as "gone".)
        names = list(app._publish_snapshots.keys())
        app._spotcheck_idx = names.index(BIN_ENT)

        app._spotcheck_republish()

        self.assertGreater(len(calls_for(app, BIN_ENT)), count_before)
        self.assertIn(BIN_ENT, app._publish_snapshots)  # cleared, then rebuilt by the heal

    def test_rotates_through_published_entities_across_ticks(self):
        app = make_app()
        app._recompute_all()
        names = list(app._publish_snapshots.keys())
        self.assertGreaterEqual(len(names), 2)

        app._spotcheck_idx = 0
        app._spotcheck_republish()
        self.assertEqual(app._spotcheck_idx, 1)
        app._spotcheck_republish()
        self.assertEqual(app._spotcheck_idx % len(names), 0)

    def test_periodic_tick_wires_in_the_spotcheck(self):
        """The 90s safety-net tick (_periodic) must call the spot-check, not just the
        plain recompute, or a restart-wiped entity could sit missing indefinitely
        whenever nothing environmental changes."""
        app = make_app()
        app._recompute_all()
        count_before = len(calls_for(app, BIN_ENT))

        names = list(app._publish_snapshots.keys())
        app._spotcheck_idx = names.index(BIN_ENT)

        app._periodic()

        self.assertGreater(len(calls_for(app, BIN_ENT)), count_before)


class RoomStateAlwaysIncludesState(unittest.TestCase):
    """2026-07-27 fix: the room_ent "unchanged snapshot" branch used to call set_state
    with attributes only (no state=) - a recreated entity (HA restart wiped it) would
    come back with attributes but no state string until something eventually changed
    the snapshot tuple."""

    def test_unchanged_snapshot_still_passes_state(self):
        app = make_app()
        app._recompute_all()  # first call: the "changed" branch (state included already)
        first = calls_for(app, ROOM_ENT)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[-1][1].get("state"), "Empty (Dark)")

        app._recompute_all()  # second call: snap unchanged -> the "else" branch

        second = calls_for(app, ROOM_ENT)
        self.assertEqual(len(second), 2)
        self.assertEqual(second[-1][1].get("state"), "Empty (Dark)")


# Real-shaped zone (the family room's live numbers) so _decide runs its actual branches
# rather than the always_dark short-circuit the older tests use.
BAND_ZONE = {
    "familyish": {
        "sensors": ["sensor.a_illuminance"],
        "outdoor_dark": 2500,
        "outdoor_bright": 8000,
        "indoor_min_bright": 280,
    }
}


def make_band_app(outdoor_lux, indoor_daylight, factor=None, raw=None, dark_fraction=None,
                   gloomy=(False, ""), extra_args=None):
    """App whose only live inputs are the smoothed outdoor lux and the zone's indoor
    daylight/raw lux. _gloomy and the sun gate are neutralised so the outdoor-band branch is
    what is under test. ``raw`` (the lamp-uncorrected indoor mean fed to the DARK vote)
    defaults to ``indoor_daylight`` when not given, so existing calls are unaffected."""
    args = {"zones": dict(BAND_ZONE)}
    if factor is not None:
        args["indoor_band_bright_factor"] = factor
    if dark_fraction is not None:
        args["indoor_band_dark_fraction"] = dark_fraction
    if extra_args:
        args.update(extra_args)
    app = make_app(args=args)
    app._sun_elevation = lambda: 30.0          # well clear of the dusk cut-off
    app._outdoor_smoothed = lambda: outdoor_lux
    app._outdoor_valid = lambda: True
    app._gloomy = lambda out=None, elev=None: gloomy
    app._zone_daylight = lambda zone: indoor_daylight
    app._zone_indoor = lambda zone: indoor_daylight if raw is None else raw
    return app


class OutdoorBandRoomDecides(unittest.TestCase):
    """2026-08-12: at 06:20 the pyranometer-derived "outdoor lux" read 3037 - inside the
    2500-8000 hold band - so the zone stayed DARK from the night and every family-room lamp
    came on, while the room's own meters measured 500-650lx against a 280lx bar. The sky gate
    is least trustworthy exactly there (a horizontal pyranometer collapses with a low sun), so
    a room that is clearly bright gets the casting vote.

    The mirror-image incident (2026-09): a cloudy dusk sat at 95-166lx - well under the
    family room's 168lx dark floor - for 40+ minutes while the sky sensor lingered in-band
    and the zone stayed "bright" from earlier in the day. The band vote now also lets a
    clearly dark room demote to DARK, reading the RAW indoor mean (not lamp-corrected) so a
    lamp cannot help manufacture a dark reading."""

    def _decide(self, app):
        return app._decide("familyish")

    def test_incident_replay_band_with_bright_room_goes_bright(self):
        target, reason = self._decide(make_band_app(3037, 573))
        self.assertEqual(target, dc.BRIGHT)
        self.assertIn("room decides", reason)

    def test_band_with_dim_room_still_holds(self):
        """Just over the bare bar is not enough - the margin exists to stop edge flapping."""
        target, _ = self._decide(make_band_app(3037, 300))
        self.assertIsNone(target)

    def test_band_with_dark_room_goes_dark(self):
        target, reason = self._decide(make_band_app(3037, 50))
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    def test_below_dark_threshold_is_untouched(self):
        """A genuinely dark sky must still win regardless of the indoor reading."""
        target, _ = self._decide(make_band_app(500, 900))
        self.assertEqual(target, dc.DARK)

    def test_factor_is_configurable(self):
        target, _ = self._decide(make_band_app(3037, 300, factor=1.0))
        self.assertEqual(target, dc.BRIGHT)

    def test_no_indoor_data_holds_as_before(self):
        app = make_band_app(3037, None)
        target, reason = app._decide("familyish")
        self.assertIsNone(target)
        self.assertIn("holding", reason)

    def test_incident_replay_cloudy_dusk_goes_dark(self):
        """The actual 2026-09 incident numbers: outdoor 3029lx (in-band), indoor 99.7lx."""
        target, reason = self._decide(make_band_app(3029, 99.7))
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    # Boundary tests at indoor_min_bright=280: dark floor 280*0.6=168, bright bar 280*1.5=420.
    def test_boundary_just_under_dark_floor_is_dark(self):
        target, reason = self._decide(make_band_app(3037, 167.9))
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    def test_boundary_at_dark_floor_holds(self):
        target, _ = self._decide(make_band_app(3037, 168.0))
        self.assertIsNone(target)

    def test_boundary_mid_band_holds(self):
        target, _ = self._decide(make_band_app(3037, 300))
        self.assertIsNone(target)

    def test_boundary_just_under_bright_bar_holds(self):
        target, _ = self._decide(make_band_app(3037, 419.9))
        self.assertIsNone(target)

    def test_boundary_at_bright_bar_is_bright(self):
        target, reason = self._decide(make_band_app(3037, 420.0))
        self.assertEqual(target, dc.BRIGHT)
        self.assertIn("room decides", reason)

    # Outdoor-endpoint inclusivity: the hold band is [outdoor_dark, outdoor_bright] - both
    # ends belong to the band vote, not to the strict outdoor_dark/outdoor_bright rules.
    def test_outdoor_at_dark_edge_with_dim_room_goes_dark_via_band(self):
        target, reason = self._decide(make_band_app(2500, 50))
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    def test_outdoor_at_bright_edge_with_dim_room_goes_dark_via_band(self):
        target, reason = self._decide(make_band_app(8000, 50))
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    def test_outdoor_just_above_bright_edge_with_dim_room_uses_existing_facade_rule(self):
        """Regression check: just past outdoor_bright is a different branch (the
        indoor_dark_fraction facade check) and must keep working unchanged."""
        target, reason = self._decide(make_band_app(8001, 50))
        self.assertEqual(target, dc.DARK)
        self.assertIn("blinds/facade", reason)

    # Disabled votes: each fraction independently gates its own half of the vote.
    def test_bright_vote_disabled_dark_floor_still_fires(self):
        target, reason = self._decide(make_band_app(3037, 50, factor=0))
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    def test_dark_vote_disabled_holds(self):
        target, reason = self._decide(make_band_app(3037, 50, dark_fraction=0))
        self.assertIsNone(target)
        self.assertIn("holding", reason)

    def test_missing_sun_elevation_still_allows_dark_vote(self):
        """The DARK vote must not require a known sun elevation."""
        app = make_band_app(3037, 50)
        app._sun_elevation = lambda: None
        target, reason = self._decide(app)
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    def test_gloomy_dark_rule_takes_precedence_over_band_vote(self):
        """Gloomy raises outdoor_dark itself (rule 3, evaluated before the band code is
        even reached) - it must win over the band vote, not merge with it."""
        app = make_band_app(
            3037, 900,
            gloomy=(True, "overcast"),
            extra_args={"gloomy_dark_multiplier": 2.2},
        )
        target, reason = self._decide(app)
        self.assertEqual(target, dc.DARK)
        self.assertIn("[gloomy: overcast]", reason)
        self.assertNotIn("room decides", reason)


LAMP_BAND_ZONE = {
    "lampish": {
        "sensors": ["sensor.lampish_illuminance"],
        "outdoor_dark": 2500,
        "outdoor_bright": 8000,
        "indoor_min_bright": 280,
        "lights": ["light.lampish_light"],
        "light_on_lux_offset": 120,
    }
}


def make_lamp_band_app(outdoor_lux, raw_lux, light_on):
    """Like make_band_app, but drives the REAL _zone_daylight/_zone_indoor from a cached
    sensor reading and a light's on/off state, to prove the DARK vote reads _zone_indoor
    (raw) rather than _zone_daylight (lamp-corrected)."""
    states = {"light.lampish_light": "on" if light_on else "off"}
    app = make_app(args={"zones": dict(LAMP_BAND_ZONE)}, states=states)
    app._indoor["sensor.lampish_illuminance"] = raw_lux
    app._sun_elevation = lambda: 30.0
    app._outdoor_smoothed = lambda: outdoor_lux
    app._outdoor_valid = lambda: True
    app._gloomy = lambda out=None, elev=None: (False, "")
    return app


class OutdoorBandDarkVoteUsesRawIndoor(unittest.TestCase):
    """The DARK vote must read the lamp-uncorrected indoor mean: a lamp's own contribution
    may never help manufacture a "dark" reading."""

    def test_light_off_dim_room_goes_dark(self):
        app = make_lamp_band_app(3037, 50, light_on=False)
        target, reason = app._decide("lampish")
        self.assertEqual(target, dc.DARK)
        self.assertIn("room decides", reason)

    def test_light_on_lamp_corrected_daylight_low_but_raw_not_low_holds(self):
        """raw=200 is above the 168lx dark floor; only the lamp-corrected daylight (80,
        after subtracting the 120lx offset) is low. Must NOT go dark from this vote."""
        app = make_lamp_band_app(3037, 200, light_on=True)
        target, reason = app._decide("lampish")
        self.assertIsNone(target)
        self.assertIn("holding", reason)


OVERLAP_ZONE = {
    "overlapish": {
        "sensors": ["sensor.overlapish_illuminance"],
        "outdoor_dark": 2500,
        "outdoor_bright": 8000,
        "indoor_min_bright": 280,
        "indoor_band_bright_factor": 0.5,
        "indoor_band_dark_fraction": 0.6,
    }
}


class IndoorBandOverlapWarning(unittest.TestCase):
    """indoor_band_bright_factor <= indoor_band_dark_fraction means the two vote thresholds
    overlap or invert (the "dark floor" would sit at or above the "bright bar"); this is a
    misconfiguration, flagged at init rather than silently misclassifying."""

    def test_overlap_logs_warning_at_init(self):
        app = make_app(args={"zones": dict(OVERLAP_ZONE)})
        warnings = [msg for msg, kw in app.log_calls if kw.get("level") == "WARNING"]
        self.assertTrue(
            any("overlapish" in msg and "votes overlap" in msg for msg in warnings),
            warnings,
        )

    def test_overlap_bright_wins_over_dark_in_the_gap(self):
        """bright_factor 0.5 -> bar 140; dark_fraction 0.6 -> floor 168; indoor 150 sits in
        the inverted gap where both could theoretically fire - BRIGHT is checked first."""
        app = make_app(args={"zones": dict(OVERLAP_ZONE)})
        app._sun_elevation = lambda: 30.0
        app._outdoor_smoothed = lambda: 3037
        app._outdoor_valid = lambda: True
        app._gloomy = lambda out=None, elev=None: (False, "")
        app._zone_daylight = lambda zone: 150
        app._zone_indoor = lambda zone: 150
        target, reason = app._decide("overlapish")
        self.assertEqual(target, dc.BRIGHT)
        self.assertIn("room decides", reason)


if __name__ == "__main__":
    unittest.main()
