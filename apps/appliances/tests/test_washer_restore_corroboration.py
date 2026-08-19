# tests/test_washer_restore_corroboration.py - FIX 1 (restore is a hypothesis) end to end.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# The 2026-08-19 incident: during an HA upgrade restart storm a stale durable-store record
# (Running, start 12:01) was restored onto a machine that had already finished (14:44) and been
# emptied (door open 14:46). The restore had no live-signal check (0W restored as Running), and a
# backstop then published Unemptied + a full Sonos announcement at 14:55 - 9 min AFTER the user
# had emptied it. FIX 1 makes a restore from a NON-live source a hypothesis: corroborate against
# live power / a recent last_high_energy_at, and if uncorroborated suppress announcements and
# reconcile ~60s later.
#
# These run the REAL initialize() end to end (real _restore_running_state, _resolve_store_candidate,
# the corroboration block, _finalize_restored_cycle_identity), same boundary as
# test_washer_restart_survival.py: only the AppDaemon surface + notifier apps are faked, and the
# state_file is always inside a tmpdir. Never stub the restore/transition delegates.
#
# D1 (2026-08-19 adversarial pass): a store-resolved Unemptied boot restored neither
# last_high_energy_at nor start_time, so _finish_anchor()'s FIX-4 floor collapsed to
# "now - anti_crease_window_minutes" (8min) and missed an older ajar emptying. Covered below:
# restoring both fields from the payload when present (RestoredUnemptiedRestoresFinishAnchor),
# and the _store_state_since fallback (restored separately, unconditionally, unchanged by this
# fix) when the payload lacks them (RestoredUnemptiedStoreStateSinceFallback).

from __future__ import annotations

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

import washer_monitor as wm  # noqa: E402
import cycle_store as cystore  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 19, 14, 55, 0, tzinfo=UTC)


def _iso(dt):
    return cystore.format_utc(dt)


def store_payload(*, state="Running", start_time=None, last_high_energy_at=None,
                  code_fingerprint=None, notification_sent=False, cycle_id="cycle-x",
                  state_since=None):
    payload = {
        "state": state,
        "start_time": _iso(start_time) if start_time else "",
        "cycle_id": cycle_id,
        "notification_sent": notification_sent,
        "detected_programme": "eco",
        "energy_at_start": 0.10,
        "started_by": "mikkel",
        "started_by_method": "sole_occupant",
    }
    if last_high_energy_at is not None:
        payload["last_high_energy_at"] = _iso(last_high_energy_at)
    if code_fingerprint is not None:
        payload["code_fingerprint"] = code_fingerprint
    if state_since is not None:
        payload["state_since"] = _iso(state_since)
    return payload


def make_init_app(*, payload=None, helper_state=None, power_watts=0.0,
                  door_history=None, door_now=None, now=NOW):
    """WasherMonitor with initialize() run for real, plus recording Sonos/Mobile notifier apps
    and an optional door history series (keyed on the door sensor for get_history)."""
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.now = now
    app._now_utc = lambda: app.now

    tmpdir = tempfile.mkdtemp(prefix="washer_restore_corrob_")
    state_file = os.path.join(tmpdir, "washer_cycle_state.json")
    if payload is not None:
        cystore.CycleStore(state_file, "washer").save(payload)

    args = {
        "power_sensor": "sensor.washer_plug_power",
        "energy_sensor": "sensor.washer_plug_energy",
        "door_sensor": "binary_sensor.washer_door_contact",
        "state_entity": "sensor.washer_state",
        "ui_state_entity": "input_select.washer_state",
        "start_w": 18,
        "stop_w": 3.0,
        "feedback_file": "/nonexistent/washer_feedback_test.json",
        "state_file": state_file,
    }
    app.args = args
    app.AD = None

    app.states = {args["power_sensor"]: power_watts}
    if helper_state is not None:
        app.states[args["ui_state_entity"]] = helper_state
    if door_now is not None:
        app.states[args["door_sensor"]] = door_now
    app.attrs_store = {}
    app.last_changed_store = {}

    app._history = {}
    if door_history is not None:
        app._history[args["door_sensor"]] = door_history

    def get_state(entity, attribute=None, **kw):
        if attribute == "all":
            if entity not in app.states and entity not in app.attrs_store:
                return None
            return {
                "state": app.states.get(entity),
                "attributes": dict(app.attrs_store.get(entity, {})),
                "last_changed": app.last_changed_store.get(entity),
                "last_updated": app.last_changed_store.get(entity),
            }
        if attribute:
            return (app.attrs_store.get(entity, {}) or {}).get(attribute)
        return app.states.get(entity)

    app.get_state = get_state

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        if state is not None:
            app.states[entity] = state
        if attributes is not None:
            if replace:
                app.attrs_store[entity] = dict(attributes)
            else:
                app.attrs_store.setdefault(entity, {}).update(attributes)
        app.last_changed_store[entity] = _iso(app._now_utc())

    app.set_state = set_state
    app.get_history = lambda entity_id=None, **kw: [list(app._history.get(entity_id, []))]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app.scheduled = []

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    app.run_in = run_in
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None

    app.sonos_calls = []
    app.mobile_calls = []
    sonos = types.SimpleNamespace(notify=lambda message: app.sonos_calls.append(message))
    mobile = types.SimpleNamespace(notify=lambda **kw: app.mobile_calls.append(kw) or "coro")

    def get_app(name):
        if name == "SonosNotifier":
            return sonos
        if name == "MobileNotifier":
            return mobile
        return None

    app.get_app = get_app
    app.create_task = lambda coro: None

    app.initialize()
    return app


def scheduled_named(app, name):
    return [(cb, delay) for cb, delay, _kw in app.scheduled if getattr(cb, "__name__", "") == name]


class IncidentReplayConvergesToEmptied(unittest.TestCase):
    def test_corroborated_restore_never_announces_and_routes_to_emptied(self):
        """Test 1: store Running (start 168 min ago), entity gone, mirror stale, 0W, but
        last_high_energy 5 min ago (corroborated). A door-open edge 3 min ago is in history. The
        restore must not announce; when the backstop then fires, FIX 2 routes to Emptied - the
        machine was emptied while we were slow - never announcing Unemptied."""
        app = make_init_app(
            payload=store_payload(
                state="Running",
                start_time=NOW - timedelta(minutes=168),
                last_high_energy_at=NOW - timedelta(minutes=5),
            ),
            helper_state="Off",
            power_watts=0.0,
            door_history=[
                {"state": "off", "last_changed": _iso(NOW - timedelta(minutes=200))},
                {"state": "on", "last_changed": _iso(NOW - timedelta(minutes=3))},
            ],
        )
        # Restored a live-again Running; last_high 5 min ago corroborated it (no suppression).
        self.assertEqual(app.state, "Running")
        self.assertFalse(app.restored_uncorroborated)
        self.assertEqual(app.sonos_calls, [])  # nothing announced during restore

        # The backstop fires: the human already opened the door 3 min ago.
        app._pending_end_reason = "standby_backstop"
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])   # never announced Unemptied
        self.assertEqual(app.mobile_calls, [])


class UncorroboratedRestoreThenPowerResumes(unittest.TestCase):
    def test_flag_set_at_boot_then_cleared_by_live_power(self):
        """Test 2: store Running restored at 0W with a stale last_high (100 min ago) - no live
        signal, so uncorroborated: a reconcile is scheduled and nothing is announced. A fresh
        sample >= start_w then clears the flag and cancels the reconcile; a subsequent real
        finish is allowed to announce."""
        app = make_init_app(
            payload=store_payload(
                state="Running",
                start_time=NOW - timedelta(minutes=100),
                last_high_energy_at=NOW - timedelta(minutes=100),
            ),
            helper_state="Off",
            power_watts=0.0,
        )
        self.assertEqual(app.state, "Running")
        self.assertTrue(app.restored_uncorroborated)
        self.assertEqual(len(scheduled_named(app, "_restore_reconcile")), 1)
        self.assertEqual(app.sonos_calls, [])

        # Live power resumes above start current -> corroborated, flag cleared.
        app._power_changed(app.power_sensor, None, None, "1500", {})
        self.assertFalse(app.restored_uncorroborated)

        # A real finish now announces (flag no longer suppresses).
        app.last_high_energy_at = NOW
        app._pending_end_reason = "standby_backstop"
        app.notification_sent = False
        app._transition_to_unemptied()
        self.assertEqual(app.state, "Unemptied")
        self.assertEqual(app.sonos_calls, ["Washer is ready to be emptied"])


class FingerprintMismatchForcesUncorroborated(unittest.TestCase):
    def test_mismatched_fingerprint_takes_the_uncorroborated_path(self):
        """Test 8: even with a RECENT last_high (which would otherwise corroborate), a durable
        store written by a different code fingerprint is treated as uncorroborated."""
        app = make_init_app(
            payload=store_payload(
                state="Running",
                start_time=NOW - timedelta(minutes=100),
                last_high_energy_at=NOW - timedelta(minutes=2),  # would corroborate on its own
                code_fingerprint="deadbeefdeadbeefdeadbeefdeadbeef",
            ),
            helper_state="Off",
            power_watts=0.0,
        )
        self.assertEqual(app.state, "Running")
        self.assertTrue(app.restored_uncorroborated)
        self.assertEqual(len(scheduled_named(app, "_restore_reconcile")), 1)
        self.assertEqual(app.sonos_calls, [])

    def test_matching_fingerprint_with_recent_high_is_corroborated(self):
        """Control for test 8: the SAME payload but stamped with this monitor's real fingerprint
        (the md5 the app itself computes over washer_monitor.py) corroborates via the recent
        last_high - proving the mismatch, not the mere presence of the field, is what forces the
        uncorroborated path."""
        import hashlib
        real_fp = hashlib.md5(Path(wm.__file__).read_bytes()).hexdigest()
        app = make_init_app(
            payload=store_payload(
                state="Running",
                start_time=NOW - timedelta(minutes=100),
                last_high_energy_at=NOW - timedelta(minutes=2),
                code_fingerprint=real_fp,
            ),
            helper_state="Off",
            power_watts=0.0,
        )
        self.assertEqual(app.state, "Running")
        self.assertFalse(app.restored_uncorroborated)


class RestoredUnemptiedRestoresFinishAnchor(unittest.TestCase):
    def test_store_last_high_and_start_time_let_first_recheck_catch_an_older_ajar_edge(self):
        """Test i (D1): store-resolved Unemptied restores last_high_energy_at (40 min old) and
        start_time from the payload, so _finish_anchor() has a real floor for the FIX-4 door
        recheck instead of collapsing to now-8min. An ajar door edge 11 min ago (older than the
        8min fallback window, but well after the restored anchor) is then correctly caught on
        the very first recheck - no announcement (Emptied never announces)."""
        app = make_init_app(
            payload=store_payload(
                state="Unemptied",
                start_time=NOW - timedelta(minutes=220),
                last_high_energy_at=NOW - timedelta(minutes=40),
            ),
            door_now="off",  # ajar: reads closed now
            door_history=[
                {"state": "off", "last_changed": _iso(NOW - timedelta(minutes=300))},
                {"state": "on", "last_changed": _iso(NOW - timedelta(minutes=11))},
            ],
        )
        self.assertEqual(app.state, "Unemptied")
        # Both fields actually landed on self, from the store payload (item 2's scoped restore).
        self.assertEqual(app.last_high_energy_at, NOW - timedelta(minutes=40))
        self.assertEqual(app.start_time, NOW - timedelta(minutes=220))

        app._unemptied_door_recheck({})
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])


class RestoredUnemptiedStoreStateSinceFallback(unittest.TestCase):
    def test_state_since_fallback_catches_edge_after_unemptied_entry(self):
        """Test ii (D1): the payload lacks BOTH last_high_energy_at and start_time, so the item-2
        restore has nothing to assign - _finish_anchor() falls through to step (c):
        self._store_state_since (restored separately and unconditionally, unchanged by this fix -
        see initialize()'s existing state_since restore). Any door edge after Unemptied entry is
        still definitely an emptying."""
        app = make_init_app(
            payload=store_payload(
                state="Unemptied",
                start_time=None,
                last_high_energy_at=None,
                state_since=NOW - timedelta(minutes=50),
            ),
            door_now="off",
            door_history=[
                {"state": "off", "last_changed": _iso(NOW - timedelta(minutes=200))},
                {"state": "on", "last_changed": _iso(NOW - timedelta(minutes=11))},
            ],
        )
        self.assertEqual(app.state, "Unemptied")
        self.assertIsNone(app.last_high_energy_at)
        self.assertIsNone(app.start_time)
        self.assertEqual(app._store_state_since, NOW - timedelta(minutes=50))

        app._unemptied_door_recheck({})
        self.assertEqual(app.state, "Emptied")
        self.assertEqual(app.sonos_calls, [])


if __name__ == "__main__":
    unittest.main()
