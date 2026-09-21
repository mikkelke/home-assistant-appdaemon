# tests/test_appliance_force_emptied.py - the dashboard "Emptied" button.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# 2026-08-07: the appliance cards grew a one-tap Emptied action for when the door contact
# misses the emptying (the documented dishwasher gap from July). The dishwasher already had
# dishwasher_force_emptied; washer_force_emptied / dryer_force_emptied are new mirrors of it.
# Contract under test: the handler transitions ONLY from Unemptied (anything earlier may be a
# live cycle whose feedback must not be saved yet) and otherwise logs a warning and does
# nothing.
#
# The dryer suite reuses test_dryer_emptied_exit's harness so _transition_to_emptied runs FOR
# REAL (this repo's incident history says stubbing the delegate hides caller/delegate bugs).
# The washer's transition needs the full attribution/feedback plumbing, so its tests pin the
# handler's guard-and-delegate contract with a recorded delegate instead - the transition
# itself is exercised by the door-open path in production daily.

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

import washer_monitor as wm  # noqa: E402
from test_dryer_emptied_exit import make_app  # noqa: E402


def make_unemptied_dryer():
    """Dryer sitting in Unemptied with the cooling clock stamped - the exact state the
    dashboard button fires from."""
    app = make_app()
    app.states[app.state_entity] = "Unemptied"
    app.state = "Unemptied"
    app.last_state_change = app._now_utc()
    return app


class DryerForceEmptied(unittest.TestCase):
    def test_from_unemptied_transitions_to_emptied_for_real(self):
        app = make_unemptied_dryer()
        app._handle_force_emptied("dryer_force_emptied", {"reason": "Dashboard"}, {})
        self.assertEqual(app.state, "Emptied")
        emptied_calls = [c for c in app.set_state_calls if c.get("state") == "Emptied"]
        self.assertEqual(len(emptied_calls), 1)
        self.assertIn("Forced emptied (Dashboard)", emptied_calls[0]["attributes"]["reason"])

    def test_ignored_while_running(self):
        app = make_app()
        app.states[app.state_entity] = "Running"
        app.state = "Running"
        app.last_state_change = app._now_utc()
        app._handle_force_emptied("dryer_force_emptied", {}, {})
        self.assertEqual(app.state, "Running")
        self.assertFalse([c for c in app.set_state_calls if c.get("state") == "Emptied"])
        self.assertTrue(any("Force Emptied ignored" in str(a[0]) for a, _ in app.log_calls))

    def test_none_data_uses_default_reason(self):
        app = make_unemptied_dryer()
        app._handle_force_emptied("dryer_force_emptied", None, {})
        self.assertEqual(app.state, "Emptied")
        emptied_calls = [c for c in app.set_state_calls if c.get("state") == "Emptied"]
        self.assertIn("Forced via event", emptied_calls[0]["attributes"]["reason"])


def make_washer(state):
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.state = state
    app.log_calls = []
    app.transitions = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app._transition_to_emptied = lambda reason, actor_user_id=None: app.transitions.append((reason, actor_user_id))
    return app


class WasherForceEmptied(unittest.TestCase):
    """washer_force_emptied (legacy event, kept for Developer Tools) - carries no HA
    context, so it never threads an actor_user_id through."""

    def test_from_unemptied_delegates_with_reason(self):
        app = make_washer("Unemptied")
        app._handle_force_emptied("washer_force_emptied", {"reason": "Dashboard"}, {})
        self.assertEqual(app.transitions, [("Forced emptied (Dashboard)", None)])

    def test_ignored_while_running(self):
        app = make_washer("Running")
        app._handle_force_emptied("washer_force_emptied", {"reason": "Dashboard"}, {})
        self.assertEqual(app.transitions, [])
        self.assertTrue(any("Force Emptied ignored" in str(a[0]) for a, _ in app.log_calls))

    def test_none_data_uses_default_reason(self):
        app = make_washer("Unemptied")
        app._handle_force_emptied("washer_force_emptied", None, {})
        self.assertEqual(app.transitions, [("Forced emptied (Forced via event)", None)])


class WasherEmptiedButton(unittest.TestCase):
    """input_button.washer_emptied (HA-native replacement, see _on_emptied_button): the
    HA context.user_id behind the tap must reach _transition_to_emptied as
    actor_user_id, and a restart replaying the button's last press must never be read
    as a fresh one."""

    @staticmethod
    def _event(old_state, new_state):
        return {"entity_id": "input_button.washer_emptied", "old_state": old_state, "new_state": new_state}

    def test_press_from_unemptied_threads_user_id(self):
        app = make_washer("Unemptied")
        old_state = {"state": "2026-09-20T10:00:00+00:00"}
        new_state = {"state": "2026-09-21T09:00:00+00:00", "context": {"user_id": "abc123"}}
        app._on_emptied_button("state_changed", self._event(old_state, new_state), {})
        self.assertEqual(app.transitions, [("Forced emptied (Dashboard button)", "abc123")])

    def test_press_with_no_context_threads_none(self):
        app = make_washer("Unemptied")
        old_state = {"state": "2026-09-20T10:00:00+00:00"}
        new_state = {"state": "2026-09-21T09:00:00+00:00"}  # no "context" key at all
        app._on_emptied_button("state_changed", self._event(old_state, new_state), {})
        self.assertEqual(app.transitions, [("Forced emptied (Dashboard button)", None)])

    def test_ignored_while_running(self):
        app = make_washer("Running")
        old_state = {"state": "2026-09-20T10:00:00+00:00"}
        new_state = {"state": "2026-09-21T09:00:00+00:00", "context": {"user_id": "abc123"}}
        app._on_emptied_button("state_changed", self._event(old_state, new_state), {})
        self.assertEqual(app.transitions, [])

    def test_first_ever_observation_with_no_old_state_is_not_a_press(self):
        """old_state is None the very first time AppDaemon observes this entity (e.g.
        right after this listener is registered) - never a real press."""
        app = make_washer("Unemptied")
        new_state = {"state": "2026-09-21T09:00:00+00:00", "context": {"user_id": "abc123"}}
        app._on_emptied_button("state_changed", self._event(None, new_state), {})
        self.assertEqual(app.transitions, [])

    def test_restart_replaying_unavailable_to_last_press_is_not_a_fresh_press(self):
        """After an HA/AppDaemon restart, input_button.* goes unavailable then restores
        its last-press timestamp - both are state_changed events and neither is a fresh
        press."""
        app = make_washer("Unemptied")
        went_unavailable = self._event({"state": "2026-09-20T10:00:00+00:00"}, {"state": "unavailable"})
        app._on_emptied_button("state_changed", went_unavailable, {})
        restored = self._event({"state": "unavailable"}, {"state": "2026-09-20T10:00:00+00:00"})
        app._on_emptied_button("state_changed", restored, {})
        self.assertEqual(app.transitions, [])

    def test_unchanged_state_is_not_a_press(self):
        app = make_washer("Unemptied")
        same = {"state": "2026-09-20T10:00:00+00:00", "context": {"user_id": "abc123"}}
        app._on_emptied_button("state_changed", self._event(same, same), {})
        self.assertEqual(app.transitions, [])

    def test_none_data_does_not_raise(self):
        app = make_washer("Unemptied")
        try:
            app._on_emptied_button("state_changed", None, {})
        except Exception as e:  # pragma: no cover
            self.fail(f"_on_emptied_button raised: {e}")
        self.assertEqual(app.transitions, [])


if __name__ == "__main__":
    unittest.main()
