# tests/test_radio_watchdog_seeding.py - 2026-07-27 deploy-blindness fix.
#
# _playing_since/_last_content used to be populated only by _on_player state-change
# callbacks while playing, so a stream already playing across an AD restart (which kills all
# in-memory state but not playback) had no entry: _check_dead_async then bailed and the
# watchdog stayed blind until a human restarted the stream. initialize() now seeds both from
# current state for players already playing at app start. Same appdaemon stub trick as
# apps/appliances/tests.

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta
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

import radio_watchdog as rw  # noqa: E402


def make_app(players=None):
    """RadioWatchdog with fake AppDaemon callables, without running initialize()."""
    app = rw.RadioWatchdog.__new__(rw.RadioWatchdog)
    app.players = list(players if players is not None else ["media_player.kitchen"])
    app._playing_since = {}
    app._last_content = {}
    app._last_attempt = {}

    app.states = {}   # entity -> state string
    app.attrs = {}     # (entity, attribute) -> value
    app.log_calls = []  # (level, message)

    def get_state(entity, attribute=None, **kw):
        if attribute is None:
            return app.states.get(entity)
        return app.attrs.get((entity, attribute))

    app.get_state = get_state
    app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
    return app


class SeedsFromPlayingState(unittest.TestCase):
    def test_playing_player_seeded_from_last_changed_and_content(self):
        app = make_app(players=["media_player.kitchen"])
        app.states["media_player.kitchen"] = "playing"
        app.attrs[("media_player.kitchen", "last_changed")] = "2026-07-27T09:00:00+00:00"
        app.attrs[("media_player.kitchen", "media_content_id")] = "x-sonosapi-stream:dr.dk"

        app._seed_playing_state()

        self.assertIn("media_player.kitchen", app._playing_since)
        expected = rw._parse_last_changed("2026-07-27T09:00:00+00:00")
        self.assertEqual(app._playing_since["media_player.kitchen"], expected)
        self.assertEqual(app._last_content["media_player.kitchen"], "x-sonosapi-stream:dr.dk")

    def test_debug_log_emitted_per_seeded_player(self):
        app = make_app(players=["media_player.kitchen"])
        app.states["media_player.kitchen"] = "playing"

        app._seed_playing_state()

        debug_logs = [(lvl, msg) for lvl, msg in app.log_calls if lvl == "DEBUG"]
        self.assertEqual(len(debug_logs), 1)
        self.assertIn("media_player.kitchen", debug_logs[0][1])

    def test_non_playing_player_not_seeded_and_not_logged(self):
        app = make_app(players=["media_player.kitchen"])
        app.states["media_player.kitchen"] = "idle"

        app._seed_playing_state()

        self.assertEqual(app._playing_since, {})
        self.assertEqual(app._last_content, {})
        self.assertEqual(app.log_calls, [])

    def test_missing_last_changed_falls_back_to_now(self):
        app = make_app(players=["media_player.kitchen"])
        app.states["media_player.kitchen"] = "playing"

        before = datetime.now()
        app._seed_playing_state()
        after = datetime.now()

        seeded = app._playing_since["media_player.kitchen"]
        self.assertGreaterEqual(seeded, before)
        self.assertLessEqual(seeded, after)

    def test_unparseable_last_changed_falls_back_to_now(self):
        app = make_app(players=["media_player.kitchen"])
        app.states["media_player.kitchen"] = "playing"
        app.attrs[("media_player.kitchen", "last_changed")] = "not-a-timestamp"

        before = datetime.now()
        app._seed_playing_state()
        after = datetime.now()

        seeded = app._playing_since["media_player.kitchen"]
        self.assertGreaterEqual(seeded, before)
        self.assertLessEqual(seeded, after)

    def test_last_attempt_cooldown_untouched(self):
        """Fix 2 must not seed/reset _last_attempt - the cooldown stays exactly as it was."""
        app = make_app(players=["media_player.kitchen"])
        app.states["media_player.kitchen"] = "playing"
        stamp = datetime.now() - timedelta(minutes=5)
        app._last_attempt["media_player.kitchen"] = stamp

        app._seed_playing_state()

        self.assertEqual(app._last_attempt, {"media_player.kitchen": stamp})

    def test_multiple_players_only_playing_ones_seeded(self):
        app = make_app(players=["media_player.a", "media_player.b"])
        app.states["media_player.a"] = "playing"
        app.states["media_player.b"] = "idle"

        app._seed_playing_state()

        self.assertIn("media_player.a", app._playing_since)
        self.assertNotIn("media_player.b", app._playing_since)

    def test_no_content_id_leaves_last_content_unset(self):
        app = make_app(players=["media_player.kitchen"])
        app.states["media_player.kitchen"] = "playing"
        # No media_content_id attribute stubbed -> get_state returns None.

        app._seed_playing_state()

        self.assertIn("media_player.kitchen", app._playing_since)
        self.assertNotIn("media_player.kitchen", app._last_content)


class InitializeWiresUpSeeding(unittest.TestCase):
    """Guards against a future edit that removes the initialize() -> _seed_playing_state()
    call without removing the method itself, which would silently reintroduce the deploy
    blindness this fix closes."""

    def test_initialize_seeds_currently_playing_players(self):
        app = rw.RadioWatchdog.__new__(rw.RadioWatchdog)
        app.args = {"players": ["media_player.kitchen"]}
        app.states = {"media_player.kitchen": "playing"}
        app.attrs = {("media_player.kitchen", "media_content_id"): "x-sonosapi-stream:dr.dk"}
        app.log_calls = []
        app.listen_calls = []

        def get_state(entity, attribute=None, **kw):
            if attribute is None:
                return app.states.get(entity)
            return app.attrs.get((entity, attribute))

        app.get_state = get_state
        app.log = lambda msg, level="INFO": app.log_calls.append((level, msg))
        app.get_app = lambda name: object()
        app.listen_state = lambda cb, entity, **kw: app.listen_calls.append((cb, entity, kw))

        app.initialize()

        self.assertIn("media_player.kitchen", app._playing_since)
        self.assertEqual(app._last_content["media_player.kitchen"], "x-sonosapi-stream:dr.dk")
        self.assertEqual(app._last_attempt, {})
        self.assertEqual(len(app.listen_calls), 1)


class ParseLastChanged(unittest.TestCase):
    def test_z_suffix_parsed_to_naive_local(self):
        dt = rw._parse_last_changed("2026-07-27T09:00:00Z")
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)

    def test_offset_aware_result_is_directly_comparable_to_now(self):
        """Must not raise (naive vs aware subtraction) when compared against datetime.now(),
        which is exactly how _check_dead_async uses _playing_since."""
        dt = rw._parse_last_changed("2026-01-01T00:00:00+00:00")
        delta = datetime.now() - dt
        self.assertGreater(delta.total_seconds(), 0)

    def test_none_and_garbage_return_none(self):
        self.assertIsNone(rw._parse_last_changed(None))
        self.assertIsNone(rw._parse_last_changed(""))
        self.assertIsNone(rw._parse_last_changed("not-a-timestamp"))


if __name__ == "__main__":
    unittest.main()
