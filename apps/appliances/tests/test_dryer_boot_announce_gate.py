# tests/test_dryer_boot_announce_gate.py - Boot self-heal announce gate (2026-08-19 hotfix).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# THE BUG this guards: dryer_monitor.py's boot self-heal (a restored Running cycle found
# already finished, power low, past the 80% duration guard) used to call
# _transition_to_unemptied(skip_announce=False, ...) unconditionally - a full Sonos
# announcement fired straight from a boot-time hypothesis, suppressed only by
# notification_sent restored from the on-disk store. That flag is False precisely when the
# dryer finished AND was emptied during an outage that also outlasted the store write, so a
# dryer already emptied hours ago would still announce "ready to empty" at boot. Same incident
# class as the washer's 2026-08-19 fix (restore corroboration / door-route / freshness); this
# is the minimal dryer-side stopgap: estimate the real finish time from recorder history
# (_estimate_cycle_end_from_history, unmodified, reused as-is), check for a door-open edge
# since then (_door_open_edge_since, new), and gate the announcement on how stale the
# detection is (announce_freshness_minutes, new dryer.yaml knob) otherwise.
#
# Harness: reuses the make_app() pattern from test_dryer_restart_survival.py (real
# initialize(), a get_state() double that distinguishes attribute="all" from a bare call, a
# tmpdir-scoped state_file) - see that file's module docstring for why the restore path is
# never stubbed here either. Adds: a get_history() double keyed by entity_id (energy-sensor
# history shapes a controllable _estimate_cycle_end_from_history result; door-sensor history
# supplies open edges), and SonosNotifier/MobileNotifier fakes + a no-op create_task, mirroring
# test_washer_restore_corroboration.py's make_init_app().
#
# Revert-check performed manually while writing this file (not shipped as a test): with
# dryer_monitor.py's fix stashed back to its pre-fix HEAD version (git stash on that file
# only, this test file and dryer.yaml left in place), DoorEdgeDuringOutageConvergesToEmptied
# and LatePushInsteadOfSonosWhenStale both FAIL (the old unconditional
# skip_announce=False fires Sonos in both cases) while FreshFinishStillAnnouncesNormally and
# HistoryUnavailableFailsOpenToAnnouncing both still PASS (regression guards, unaffected by
# the fix either way). `git stash pop` restored the fix afterward.

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import timedelta
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

import dryer_monitor as dm  # noqa: E402
import cycle_store as cs  # noqa: E402

STATE_ENTITY = "sensor.dryer_state"
UI_SELECT = "input_select.dryer_state"
POWER_SENSOR = "sensor.dryer_plug_power"
ENERGY_SENSOR = "sensor.dryer_plug_energy"
DOOR_SENSOR = "binary_sensor.dryer_door_contact"


def _entity_get_state(entities, call_log):
    """Same get_state() double as test_dryer_restart_survival.py - distinguishes
    attribute="all" from a bare call, which the real boot-attrs read (and therefore the real
    restore path) depends on."""

    def get_state(entity, attribute=None, **kwargs):
        rec = entities.get(entity)
        if attribute == "all":
            call_log.append(("get_state_all", entity))
            if rec is None:
                return None
            return {
                "state": rec.get("state"),
                "attributes": dict(rec.get("attributes") or {}),
                "last_changed": rec.get("last_changed"),
                "last_updated": rec.get("last_changed"),
            }
        call_log.append(("get_state", entity))
        if rec is None:
            return None
        if attribute:
            return (rec.get("attributes") or {}).get(attribute)
        return rec.get("state")

    return get_state


def make_app(tmpdir, *, sensor_state=None, helper_state=None, power_w="0",
             energy_kwh="10.0", door_state="off", extra_args=None):
    """Real DryerMonitor with initialize() run for real - see test_dryer_restart_survival.py's
    module docstring for why the restore methods are never stubbed. Adds a get_history()
    double (app._history: entity_id -> list of {"state", "last_changed"} dicts, wrapped in the
    list-of-lists shape AppDaemon's real get_history returns - see dryer_monitor.py's own
    _flatten_history) and SonosNotifier/MobileNotifier fakes, on top of the same entity/
    set_state/run_in/log doubles used there."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)

    entities = {
        STATE_ENTITY: {"state": sensor_state, "attributes": {}, "last_changed": None},
        UI_SELECT: {"state": helper_state, "attributes": {}, "last_changed": None},
        POWER_SENSOR: {"state": power_w, "attributes": {}, "last_changed": None},
        ENERGY_SENSOR: {"state": energy_kwh, "attributes": {}, "last_changed": None},
        DOOR_SENSOR: {"state": door_state, "attributes": {}, "last_changed": None},
    }
    app._test_entities = entities

    call_log = []
    app.call_log = call_log
    app.get_state = _entity_get_state(entities, call_log)

    app._history = {}
    app.get_history = lambda entity_id=None, **kw: [list(app._history.get(entity_id, []))]

    state_file = str(Path(tmpdir) / "dryer_cycle_state.json")
    args = {
        "power_sensor": POWER_SENSOR,
        "energy_sensor": ENERGY_SENSOR,
        "door_sensor": DOOR_SENSOR,
        "state_entity": STATE_ENTITY,
        "ui_state_entity": UI_SELECT,
        "start_w": 8,
        "stop_w": 5,
        "run_for": 60,
        "stop_for": 60,
        "feedback_file": str(Path(tmpdir) / "dryer_feedback_test.json"),
        "programmes_file": str(Path(tmpdir) / "dryer_programmes_test.yaml"),
        "state_file": state_file,
    }
    if extra_args:
        args.update(extra_args)
    app.args = args
    app.state_file = args["state_file"]

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

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

    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None

    app.scheduled = []

    def run_in(cb, delay, **kw):
        handle = f"timer:{len(app.scheduled)}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in

    def run_every(cb, start, interval, **kw):
        handle = f"every:{len(app.scheduled)}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, interval, kw))
        return handle

    app.run_every = run_every
    app.datetime = lambda: app._now_utc()

    app.set_state_calls = []

    def set_state(entity_id, **kw):
        call_log.append(("set_state", entity_id))
        app.set_state_calls.append((entity_id, kw))
        rec = entities.setdefault(entity_id, {"state": None, "attributes": {}, "last_changed": None})
        if "state" in kw and kw["state"] is not None:
            rec["state"] = kw["state"]
        if kw.get("attributes") is not None:
            if kw.get("replace"):
                rec["attributes"] = dict(kw["attributes"])
            else:
                rec["attributes"].update(kw["attributes"])
        rec["last_changed"] = app._now_utc()

    app.set_state = set_state

    return app, entities


def seed_store(tmpdir, appliance, payload):
    store = cs.CycleStore(Path(tmpdir) / f"{appliance}_cycle_state.json", appliance)
    assert store.save(payload)
    return store


def _energy_history_for_end(end_dt, base_energy=9.0, jump_kwh=0.05, gap_seconds=60):
    """Two energy-sensor history points whose implied wattage between them
    ((jump_kwh*1000) / (gap_seconds/3600), comfortably above dryer.yaml's default
    energy_active_watts=100W) marks a "high power" transition ending 2 minutes before end_dt -
    _estimate_cycle_end_from_history adds that same 2-minute margin back on its picked
    candidate, so this reproduces end_dt exactly."""
    jump_end_at = end_dt - timedelta(minutes=2)
    t1 = jump_end_at - timedelta(seconds=gap_seconds)
    return [
        {"state": f"{base_energy:.3f}", "last_changed": cs.format_utc(t1)},
        {"state": f"{base_energy + jump_kwh:.3f}", "last_changed": cs.format_utc(jump_end_at)},
    ]


def _door_open_entries(*open_times):
    return [{"state": "on", "last_changed": cs.format_utc(t)} for t in open_times]


def info_logs(app):
    return [str(a[0]) for a, kw in app.log_calls if kw.get("level") == "INFO"]


class DoorEdgeDuringOutageConvergesToEmptied(unittest.TestCase):
    """(a) Self-heal finds the cycle finished ~3h ago (recorder history), and the door-sensor
    recorder shows an open edge after that estimated finish - the user already emptied it
    during the outage. Must suppress the announcement entirely (no Sonos, no push) and land on
    Emptied, not Unemptied."""

    def test_door_edge_after_estimated_finish_suppresses_and_reaches_emptied(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                # Stays under max_running_hours (default 5h = 300min) so the boot-store
                # candidate itself is not rejected as stale before the self-heal ever runs.
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=200)),
                "energy_at_start": "9.0",
                "detected_programme": "unknown",
                "programme_duration_min": 120,
                "max_power_w": 850.0,
            })
            estimated_end = now - timedelta(minutes=180)
            app._history[ENERGY_SENSOR] = _energy_history_for_end(estimated_end)
            app._history[DOOR_SENSOR] = _door_open_entries(now - timedelta(minutes=150))

            app.initialize()

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(app.sonos_calls, [])
            self.assertEqual(app.mobile_calls, [])
            self.assertTrue(
                any("door-edge" in msg for msg in info_logs(app)),
                "expected an INFO log naming the door-edge gate",
            )


class LatePushInsteadOfSonosWhenStale(unittest.TestCase):
    """(b) Self-heal finds the cycle finished ~3h ago and no door activity since - stale enough
    (> announce_freshness_minutes, default 20) that a mobile push ("finished ~N min ago")
    replaces the Sonos announcement. notification_sent must still end up True so nothing else
    re-announces this same cycle later."""

    def test_no_door_activity_and_stale_finish_pushes_instead_of_announcing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=200)),
                "energy_at_start": "9.0",
                "detected_programme": "unknown",
                "programme_duration_min": 120,
                "max_power_w": 850.0,
            })
            estimated_end = now - timedelta(minutes=180)
            app._history[ENERGY_SENSOR] = _energy_history_for_end(estimated_end)
            # No door history at all - _door_open_edge_since must see no edge.

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(app.sonos_calls, [])
            self.assertEqual(len(app.mobile_calls), 1)
            self.assertIn("180 min ago", app.mobile_calls[0].get("message", ""))
            self.assertTrue(app.notification_sent)
            self.assertTrue(
                any("late-push" in msg for msg in info_logs(app)),
                "expected an INFO log naming the late-push gate",
            )


class FreshFinishStillAnnouncesNormally(unittest.TestCase):
    """(c) Regression guard: a finish detected only ~4 minutes late (e.g. HA restarted right as
    the dryer finished) is fresh enough that today's behavior must be completely unchanged -
    full Sonos announcement, no push."""

    def test_fresh_finish_announces_exactly_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=150)),
                "energy_at_start": "9.0",
                "detected_programme": "unknown",
                "programme_duration_min": 120,
                "max_power_w": 850.0,
            })
            estimated_end = now - timedelta(minutes=4)
            app._history[ENERGY_SENSOR] = _energy_history_for_end(estimated_end)

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(len(app.sonos_calls), 1)
            self.assertEqual(app.sonos_calls[0], app.announce_message)
            self.assertEqual(app.mobile_calls, [])
            self.assertTrue(app.notification_sent)
            self.assertTrue(
                any("fresh-announce" in msg for msg in info_logs(app)),
                "expected an INFO log naming the fresh-announce gate",
            )


class HistoryUnavailableFailsOpenToAnnouncing(unittest.TestCase):
    """(d) The recorder itself is unavailable (get_history raises) for both the energy and door
    lookups. _estimate_cycle_end_from_history must degrade to None (caught internally), the
    self-heal's own `or self._now_utc()` fallback then yields ~0 min latency, and
    _door_open_edge_since must degrade to False (caught internally) rather than raising and
    aborting the whole self-heal. Net effect: fail OPEN to the normal announcement, never fail
    to silence."""

    def test_recorder_failure_still_announces_never_silently_drops_the_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=150)),
                "energy_at_start": "9.0",
                "detected_programme": "unknown",
                "programme_duration_min": 120,
                "max_power_w": 850.0,
            })

            def broken_get_history(entity_id=None, **kw):
                raise RuntimeError("recorder unavailable")

            app.get_history = broken_get_history

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(len(app.sonos_calls), 1)
            self.assertEqual(app.mobile_calls, [])
            self.assertTrue(app.notification_sent)
            self.assertTrue(
                any("fresh-announce" in msg for msg in info_logs(app)),
                "history failure must fail open to the fresh-announce gate, not abort silently",
            )


if __name__ == "__main__":
    unittest.main()
