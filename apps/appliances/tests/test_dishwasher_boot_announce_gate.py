# tests/test_dishwasher_boot_announce_gate.py - Boot self-heal announce gate (2026-08-20 hotfix).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# THE BUG this guards: dishwasher_monitor.py's boot self-heal (a restored-Running cycle found
# already finished at 0W, past the finish guard) routes through _finish_with_dry_tail, whose
# IMMEDIATE branch (zero-tail programme, or the dry tail already elapsed at a late evaluation)
# used to call _transition_to_unemptied(skip_announce=False) unconditionally - a full Sonos
# announcement fired straight from a boot-time hypothesis, suppressed only by notification_sent
# restored from the on-disk store. That flag is False precisely when the machine finished AND was
# emptied during an outage that also outlasted the store write, so a dishwasher already emptied
# hours ago would still blast "ready to empty" at boot. Same incident class as the washer's and
# dryer's 2026-08-19 fixes (restore corroboration / door-route / freshness).
#
# The dishwasher-specific stopgap ports the dryer's 3-way gate to the boot self-heal only, adapted
# to this machine's physics: the end estimate anchors to last high-power + the programme's dry
# tail (NEVER raw 0W idleness - passive condensation drying draws nothing for 2+ h here, so 0W is
# not a finish; _boot_finish_end_estimate). A door-open edge since that end (_door_open_edge_since,
# new) means it was emptied during the outage -> converge silently to Emptied. Otherwise gate on
# staleness (announce_freshness_minutes, new dishwasher.yaml knob): past it, a quiet mobile push
# via the file's existing _push_mobile idiom replaces the Sonos blast; within it, unchanged. The
# DEFERRED dry-tail path (machine genuinely still drying) is untouched and still announces at the
# machine's own end - the dishwasher-specific MidTail guard below is the 2026-08-12 lesson that a
# mid-dry restore must not be force-finished early.
#
# Harness: mirrors test_dryer_boot_announce_gate.py (real initialize(), a get_state() double that
# distinguishes attribute="all", a tmpdir-scoped state_file, a get_history() double keyed by
# entity_id, SonosNotifier/MobileNotifier fakes + a no-op create_task) - see
# test_dishwasher_restart_survival.py's module docstring for why the restore path is never stubbed.
# programmes_file points at the REAL dishwasher_programmes.yaml so eco's dry_tail_minutes: 35 (the
# MidTail case) comes from the checked-in file, not a fixture copy.
#
# Revert-check (performed while writing this file, not shipped as a test): with
# dishwasher_monitor.py stashed back to its pre-fix HEAD version (git stash on that file only, this
# test and dishwasher.yaml left in place), DoorEdgeDuringOutageConvergesToEmptied and
# LatePushInsteadOfSonosWhenStale both FAIL (the old unconditional skip_announce=False fires Sonos
# in both) while FreshFinishStillAnnouncesNormally, HistoryUnavailableFailsOpenToAnnouncing and
# MidTailRestoreStillAnnouncesAtTailEnd still PASS (regression guards, unaffected by the fix).
# `git stash pop` restored the fix afterward.

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

import dishwasher_monitor as dm  # noqa: E402
import cycle_store as cs  # noqa: E402

STATE_ENTITY = "sensor.dishwasher_state"
UI_SELECT = "input_select.dishwasher_state"
POWER_SENSOR = "sensor.dishwasher_plug_power"
ENERGY_SENSOR = "sensor.dishwasher_plug_energy"
DOOR_SENSOR = "binary_sensor.dishwasher_door_contact"
REAL_PROGRAMMES = str(Path(__file__).resolve().parents[1] / "dishwasher_programmes.yaml")
ANNOUNCE = "Dishwasher is ready to be emptied"


def _entity_get_state(entities, call_log):
    """get_state() double that distinguishes attribute="all" from a bare call, which the real
    boot-attrs read (and therefore the real restore path) depends on."""

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


def make_app(tmpdir, *, helper_state="Running", power_w="0", energy_kwh="1.7",
             door_state="off", extra_args=None):
    """Real DishwasherMonitor with initialize() run for real. Adds a get_history() double
    (app._history: entity_id -> list of {"state", "last_changed"} dicts, wrapped in the
    list-of-lists shape AppDaemon's real get_history returns) and SonosNotifier/MobileNotifier
    fakes, on top of the same entity/set_state/run_in/log doubles the restart-survival harness
    uses. The store is the ONLY carrier of a restored cycle clock after a HA-restart erasure, so
    the incident is seeded there (seed_store), never stubbed onto the app."""
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)

    entities = {
        STATE_ENTITY: {"state": None, "attributes": {}, "last_changed": None},
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

    state_file = str(Path(tmpdir) / "dishwasher_cycle_state.json")
    args = {
        "power_sensor": POWER_SENSOR,
        "energy_sensor": ENERGY_SENSOR,
        "door_sensor": DOOR_SENSOR,
        "state_entity": STATE_ENTITY,
        "ui_state_entity": UI_SELECT,
        "start_w": 8,
        "stop_w": 2,
        "run_for": 60,
        "stop_for": 90,
        "feedback_file": str(Path(tmpdir) / "dishwasher_feedback_test.json"),
        "programmes_file": REAL_PROGRAMMES,
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


def seed_store(tmpdir, payload):
    store = cs.CycleStore(Path(tmpdir) / "dishwasher_cycle_state.json", "dishwasher")
    assert store.save(payload)
    return store


def running_store(now, *, start_min_ago, prog, guard_dur, last_high_min_ago):
    """A store payload that restores Running: the machine started `start_min_ago` (kept under
    max_running_hours so the boot-store candidate itself is not rejected), notification never
    sent (the store was last written while Running, before any announce)."""
    return {
        "state": "Running",
        "cycle_start_time": cs.format_utc(now - timedelta(minutes=start_min_ago)),
        "energy_at_start": "1.0",
        "detected_programme": prog,
        "expected_dur_at_start": guard_dur,
        "max_power_w": 1800.0,
        "last_high_power_time": cs.format_utc(now - timedelta(minutes=last_high_min_ago)),
        "notification_sent": False,
    }


def _door_open_entries(*open_times):
    return [{"state": "on", "last_changed": cs.format_utc(t)} for t in open_times]


def info_logs(app):
    return [str(a[0]) for a, kw in app.log_calls if kw.get("level") == "INFO"]


class DoorEdgeDuringOutageConvergesToEmptied(unittest.TestCase):
    """(b-door) The boot self-heal finds a zero-tail cycle finished ~55 min ago, and the door
    recorder shows an open edge after that estimated finish - the user already emptied it during
    the outage. Must suppress the announcement entirely (no Sonos, no push) and land on Emptied."""

    def test_door_edge_after_estimated_finish_suppresses_and_reaches_emptied(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp)
            now = app._now_utc()
            seed_store(tmp, running_store(
                now, start_min_ago=200, prog="gentle", guard_dur=149, last_high_min_ago=55))
            app._history[DOOR_SENSOR] = _door_open_entries(now - timedelta(minutes=40))

            app.initialize()

            self.assertEqual(app.state, "Emptied")
            self.assertEqual(app.sonos_calls, [])
            self.assertEqual(app.mobile_calls, [])
            self.assertTrue(
                any("door-edge" in msg for msg in info_logs(app)),
                "expected an INFO log naming the door-edge gate",
            )


class LatePushInsteadOfSonosWhenStale(unittest.TestCase):
    """(b-stale) The boot self-heal finds a zero-tail cycle finished ~115 min ago and no door
    activity since - stale enough (> announce_freshness_minutes, default 20) that a mobile push
    replaces the Sonos announcement. notification_sent must end up True so nothing re-announces."""

    def test_no_door_activity_and_stale_finish_pushes_instead_of_announcing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp)
            now = app._now_utc()
            seed_store(tmp, running_store(
                now, start_min_ago=260, prog="gentle", guard_dur=149, last_high_min_ago=115))
            # No door history at all - _door_open_edge_since must see no edge.

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(app.sonos_calls, [])
            self.assertEqual(len(app.mobile_calls), 1)
            self.assertIn("115 min ago", app.mobile_calls[0].get("message", ""))
            self.assertTrue(app.notification_sent)
            self.assertTrue(
                any("late-push" in msg for msg in info_logs(app)),
                "expected an INFO log naming the late-push gate",
            )


class FreshFinishStillAnnouncesNormally(unittest.TestCase):
    """(c/fresh) Regression guard: a zero-tail finish detected only ~3 min late (HA restarted
    right as the machine finished) is fresh enough that today's behavior is unchanged - full
    Sonos announcement, no push."""

    def test_fresh_finish_announces_exactly_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp)
            now = app._now_utc()
            seed_store(tmp, running_store(
                now, start_min_ago=155, prog="gentle", guard_dur=149, last_high_min_ago=3))

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(app.sonos_calls, [ANNOUNCE])
            self.assertEqual(app.mobile_calls, [])
            self.assertTrue(app.notification_sent)
            self.assertTrue(
                any("fresh-announce" in msg for msg in info_logs(app)),
                "expected an INFO log naming the fresh-announce gate",
            )


class HistoryUnavailableFailsOpenToAnnouncing(unittest.TestCase):
    """(fail-open) The recorder is unavailable (get_history raises) for the door lookup. The end
    estimate anchors to last high-power (not history), and _door_open_edge_since must degrade to
    False (caught internally) rather than raising and aborting the self-heal - so a fresh finish
    still fires the full Sonos announcement. A broken recorder fails OPEN, never to silence."""

    def test_recorder_failure_still_announces_never_silently_drops_the_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp)
            now = app._now_utc()
            seed_store(tmp, running_store(
                now, start_min_ago=155, prog="gentle", guard_dur=149, last_high_min_ago=3))

            def broken_get_history(entity_id=None, **kw):
                raise RuntimeError("recorder unavailable")

            app.get_history = broken_get_history

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(app.sonos_calls, [ANNOUNCE])
            self.assertEqual(app.mobile_calls, [])
            self.assertTrue(app.notification_sent)
            self.assertTrue(
                any("fresh-announce" in msg for msg in info_logs(app)),
                "history failure must fail open to the fresh-announce gate, not abort silently",
            )


class MidTailRestoreStillAnnouncesAtTailEnd(unittest.TestCase):
    """Dishwasher-specific guard (the 2026-08-12 wrong-manual-force lesson): an ECO restore where
    the machine is GENUINELY still in its passive dry (guard open, but last high-power was minutes
    ago and the 35-min dry tail has NOT elapsed) must NOT be force-finished at boot. The self-heal
    defers via the dry-tail timer, announces NOTHING at init, and the announcement fires normally
    when that timer reaches the machine's own end. The freshness gate must never touch this path."""

    def test_mid_tail_defers_at_boot_then_announces_at_real_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp)
            now = app._now_utc()
            # start 230 min ago, guard 234 -> guard opens ~8 min ago; last high-power 15 min ago;
            # eco dry_tail 35 min (real yaml) -> machine end ~27 min in the FUTURE.
            seed_store(tmp, running_store(
                now, start_min_ago=230, prog="eco", guard_dur=234, last_high_min_ago=15))
            # A door edge exists in the window but must be IRRELEVANT while still drying - the
            # gate never runs on the deferred branch.
            app._history[DOOR_SENSOR] = _door_open_entries(now - timedelta(minutes=5))

            app.initialize()

            # Deferred: still Running, nothing announced, no gate log at all.
            self.assertEqual(app.state, "Running")
            self.assertEqual(app.sonos_calls, [])
            self.assertEqual(app.mobile_calls, [])
            self.assertFalse(app.notification_sent)
            self.assertFalse(
                any("announce gate" in msg for msg in info_logs(app)),
                "the deferred (still-drying) path must never reach the boot announce gate",
            )
            tail_cbs = [cb for cb, _d, _k in app.scheduled
                        if getattr(cb, "__name__", "") == "_dry_tail_elapsed"]
            self.assertEqual(len(tail_cbs), 1, "expected a single deferred dry-tail transition")

            # The machine's own end arrives: the deferred callback fires (power still 0 W) and
            # announces normally over Sonos - exactly the behaviour this path has always had.
            tail_cbs[0]({})
            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(app.sonos_calls, [ANNOUNCE])
            self.assertEqual(app.mobile_calls, [])


if __name__ == "__main__":
    unittest.main()
