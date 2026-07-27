from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import bedroom_solar_shade as bss  # noqa: E402


def make_app(states):
    """BedroomSolarShade with a fake get_state reading from `states`, without
    running AppDaemon's initialize()."""
    app = bss.BedroomSolarShade.__new__(bss.BedroomSolarShade)
    app.person_entities = ["person.mikkel", "person.kristine"]
    app.get_state = lambda entity, **kw: states.get(entity)
    return app


class EveryoneAway(unittest.TestCase):
    """2026-07-15: while everyone's away, skip daylight balancing and close fully.
    Fail-safe is the point of this test class - an unknown/unavailable tracker (dead
    phone, lost GPS fix) must NOT be read as away, or a flaky tracker would blackout
    the room on someone who is actually home."""

    def test_true_when_both_away(self):
        app = make_app({"person.mikkel": "not_home", "person.kristine": "not_home"})
        self.assertTrue(app._everyone_away())

    def test_true_with_a_named_zone_not_home(self):
        app = make_app({"person.mikkel": "work", "person.kristine": "not_home"})
        self.assertTrue(app._everyone_away())

    def test_false_when_one_home(self):
        app = make_app({"person.mikkel": "home", "person.kristine": "not_home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_both_home(self):
        app = make_app({"person.mikkel": "home", "person.kristine": "home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_tracker_unknown(self):
        app = make_app({"person.mikkel": "unknown", "person.kristine": "not_home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_tracker_unavailable(self):
        app = make_app({"person.mikkel": "unavailable", "person.kristine": "not_home"})
        self.assertFalse(app._everyone_away())

    def test_false_when_tracker_missing(self):
        app = make_app({"person.kristine": "not_home"})  # mikkel key absent -> None
        self.assertFalse(app._everyone_away())


def _fresh_state_path():
    """A valid path (inside the system temp dir) that does not exist yet - lets
    _load_state's FileNotFoundError branch and _save_state's create-on-first-write both
    be exercised without leaving stray directories behind."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    return path


def make_init_app(states=None, attrs=None, args=None):
    """BedroomSolarShade built via the REAL initialize() against a fully faked AD
    surface (listen_state/run_every are no-ops; run_every's callback is captured on
    ``_tick_cb`` so a caller can simulate the "immediate" first tick firing)."""
    app = bss.BedroomSolarShade.__new__(bss.BedroomSolarShade)
    app.args = dict(args or {})
    app._states = dict(states or {})
    app._attrs = dict(attrs or {})
    app.get_state = lambda entity, attribute=None, **kw: (
        app._attrs.get(entity, {}).get(attribute) if attribute is not None else app._states.get(entity)
    )
    app.listen_state = lambda *a, **kw: None
    app._tick_cb = None

    def run_every(cb, start, interval):
        app._tick_cb = cb

    app.run_every = run_every
    app.run_in = lambda cb, delay, **kw: None
    app.log = lambda *a, **kw: None
    app.fire_event = lambda *a, **kw: None
    app.set_state_calls = []
    app.set_state = lambda entity, **kw: app.set_state_calls.append((entity, kw))
    app.call_service_calls = []
    app.call_service = lambda service, **kw: app.call_service_calls.append((service, kw))
    app.get_now = lambda: None
    app.initialize()
    return app


class StatePersistence(unittest.TestCase):
    """_load_state/_save_state schema: {"last_cmd": int|None, "override_until": iso str|None}.
    Atomic tmp+os.replace (house_events pattern), deploy_advisor's path/arg convention."""

    def _make(self, path):
        app = bss.BedroomSolarShade.__new__(bss.BedroomSolarShade)
        app.state_file = path
        app.log = lambda *a, **kw: None
        app._last_cmd = None
        app._override_until = None
        return app

    def test_save_then_load_round_trips(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        writer = self._make(path)
        writer._last_cmd = 42
        writer._override_until = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
        writer._save_state()

        with open(path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, {"last_cmd": 42, "override_until": "2026-07-27T09:00:00+00:00"})

        reader = self._make(path)
        reader._load_state()
        self.assertEqual(reader._last_cmd, 42)
        self.assertEqual(reader._override_until, writer._override_until)

    def test_missing_file_leaves_defaults(self):
        app = self._make("/nonexistent/dir/does_not_exist.json")
        app._load_state()  # must not raise
        self.assertIsNone(app._last_cmd)
        self.assertIsNone(app._override_until)

    def test_save_is_atomic_tmp_then_replace(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        app = self._make(path)
        app._last_cmd = 10
        app._save_state()
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertTrue(os.path.exists(path))


class SeededLastCmd(unittest.TestCase):
    """2026-07-27: with no persisted baseline, seed _last_cmd from the cover's current
    position at init so a manual move right after a restart is still classified as
    manual, without needing this app to have issued a command of its own first."""

    def test_seeds_from_current_position_when_nothing_persisted(self):
        path = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        app = make_init_app(
            states={"input_boolean.bedroom_solar_shade": "off"},
            attrs={"cover.bedroom_blind": {"current_position": 55}},
            args={"state_file": path},
        )

        self.assertEqual(app._last_cmd, 55)
        self.assertIsNone(app._override_until)

    def test_seeded_baseline_classifies_a_manual_move(self):
        path = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        app = make_init_app(
            states={"input_boolean.bedroom_solar_shade": "off"},
            attrs={"cover.bedroom_blind": {"current_position": 55}},
            args={"state_file": path},
        )
        app.get_now = lambda: datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)

        # Manual move to 80% right after a (simulated) restart, before this app has
        # issued any command of its own - must still pause shading.
        app._on_cover_change("cover.bedroom_blind", "current_position", 55, "80", {})

        self.assertIsNotNone(app._override_until)


class OverrideSurvivesRestart(unittest.TestCase):
    """2026-07-27: the manual-pause deadline persists across a simulated AD restart, and
    the "immediate" first tick after the restart honors it (never re-commands a hand-set
    blind) instead of overriding it because the in-memory deadline was lost."""

    def _states(self):
        return {
            "input_boolean.bedroom_solar_shade": "on",
            "input_boolean.mikkel_sleep_mode": "off",
            "input_datetime.wakeup_bedroom": "06:00",
        }

    def _attrs(self, position):
        return {
            "cover.bedroom_blind": {"current_position": position},
            "sun.sun": {"elevation": 30.0, "azimuth": 70.0},
        }

    def test_restored_override_pauses_the_first_post_restart_tick(self):
        state_file = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))

        move_time = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
        app_before = make_init_app(
            states=self._states(), attrs=self._attrs(55), args={"state_file": state_file},
        )
        app_before.get_now = lambda: move_time
        # Hand move to 80% (baseline seeded at 55 from current_position) - pauses shading.
        app_before._on_cover_change("cover.bedroom_blind", "current_position", 55, "80", {})
        self.assertIsNotNone(app_before._override_until)

        # "Restart": a fresh instance reloads state_file at init.
        app_after = make_init_app(
            states=self._states(), attrs=self._attrs(80), args={"state_file": state_file},
        )
        self.assertEqual(app_after._override_until, app_before._override_until)
        self.assertEqual(app_after._last_cmd, 55)  # persisted baseline, not the manual 80%

        # Its immediate first tick (captured via run_every) must honor the still-active
        # override rather than immediately re-commanding the cover.
        tick_time = move_time + timedelta(minutes=30)  # well within manual_pause_min (120)
        app_after.get_now = lambda: tick_time
        self.assertIsNotNone(app_after._tick_cb)
        app_after._tick_cb(None)

        self.assertEqual(app_after.call_service_calls, [])
        self.assertTrue(app_after.set_state_calls)
        last_entity, last_kwargs = app_after.set_state_calls[-1]
        self.assertEqual(last_entity, "sensor.bedroom_solar_shade_status")
        self.assertEqual(last_kwargs.get("state"), "manual")

    def test_expired_override_does_not_block_the_post_restart_tick(self):
        """An override that lapsed while AD was down must not linger - the persisted
        deadline is simply in the past, so the normal (non-manual) tick logic runs."""
        state_file = _fresh_state_path()
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))

        move_time = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
        app_before = make_init_app(
            states=self._states(), attrs=self._attrs(55), args={"state_file": state_file},
        )
        app_before.get_now = lambda: move_time
        app_before._on_cover_change("cover.bedroom_blind", "current_position", 55, "80", {})

        app_after = make_init_app(
            states=self._states(), attrs=self._attrs(80), args={"state_file": state_file},
        )
        # AD was down well past manual_pause_min (120 min) before restarting.
        app_after.get_now = lambda: move_time + timedelta(hours=6)
        app_after._tick_cb(None)

        last_entity, last_kwargs = app_after.set_state_calls[-1]
        self.assertNotEqual(last_kwargs.get("state"), "manual")


if __name__ == "__main__":
    unittest.main()
