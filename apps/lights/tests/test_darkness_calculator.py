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

    app.log = lambda *a, **kw: None
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


if __name__ == "__main__":
    unittest.main()
