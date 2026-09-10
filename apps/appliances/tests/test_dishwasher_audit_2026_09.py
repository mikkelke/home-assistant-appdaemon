# tests/test_dishwasher_audit_2026_09.py - Regression coverage for the 7 verified flaws from
# the 2026-09-07 dishwasher_monitor.py audit (see the assignment / appliance-logic-audit memo).
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# One TestCase (or small group of TestCase methods) per flaw, each written to FAIL against the
# pre-fix code and PASS once the corresponding dishwasher_monitor.py fix lands - verified by
# running this file before and after the fix, mirroring this directory's existing convention
# (see test_dishwasher_boot_announce_gate.py's own "Revert-check" note for the same practice).
#
# Two harnesses are used, both copied (not imported) from this directory's own established
# patterns, since every test file here is deliberately self-contained:
#   - make_boot_app / seed_store: real initialize(), a get_state() double that distinguishes
#     attribute="all", a get_history() double, Sonos/Mobile fakes, a tmpdir-scoped state_file -
#     copied from test_dishwasher_boot_announce_gate.py's make_app. Used for flaws 1, 2, 4
#     (all boot/self-heal interactions).
#   - make_live_app: a mid-cycle DishwasherMonitor with fake AD I/O and a movable clock, no
#     initialize() involved - copied from test_dishwasher_dry_tail.py's make_running_app. Used
#     for flaws 3 and 6 (live door/poll interactions).
#   - make_feedback_app: a minimal object exercising only the feedback-file read/write trio.
#     Used for flaw 7.
#   - make_classify_app: a minimal object exercising only _classify_programme() /
#     _load_programme_profiles() against the REAL dishwasher_programmes.yaml. Used for flaw 5.

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

import dishwasher_monitor as dm  # noqa: E402
import cycle_store as cs  # noqa: E402

STATE_ENTITY = "sensor.dishwasher_state"
UI_SELECT = "input_select.dishwasher_state"
POWER_SENSOR = "sensor.dishwasher_plug_power"
ENERGY_SENSOR = "sensor.dishwasher_plug_energy"
DOOR_SENSOR = "binary_sensor.dishwasher_door_contact"
REAL_PROGRAMMES_FILE = str(Path(__file__).resolve().parents[1] / "dishwasher_programmes.yaml")
ANNOUNCE = "Dishwasher is ready to be emptied"

# Numeric/behavioral knobs mirrored by hand from apps/appliances/dishwasher.yaml - see
# test_dishwasher_restart_survival.py's own PRODUCTION_ARGS table for the same rationale
# (reproduce the real incident shape, not a synthetic one).
PRODUCTION_ARGS = {
    "start_w": 8,
    "stop_w": 2,
    "run_for": 60,
    "stop_for": 90,
    "high_power_threshold": 2,
    "door_close_fast_start_window_s": 900,
    "start_sustain_seconds_without_door": 120,
    "power_unavailable_error_after_seconds": 180,
    "fill_window_minutes": 74,
    "min_cycle_minutes": 74,
    "min_energy_kwh": 0.4,
    "finish_guard_fraction": 0.95,
    "finish_guard_use_learned": True,
    "finish_guard_min_learned_n": 5,
    "pause_timeout_minutes": 5,
    "max_running_hours": 5,
    "unemptied_timeout_hours": 0,
    "emptied_timeout_minutes": 30,
    "min_emptying_seconds": 45,
    "energy_active_watts": 100.0,
}


# ==========================================================================================
# Harness 1: boot / initialize() - flaws 1, 2, 4 (all interact with the boot self-heal block)
# ==========================================================================================

def _entity_get_state(entities, call_log):
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


def make_boot_app(tmpdir, *, helper_state=None, power_w="0", energy_kwh="1.7",
                   door_state="off", extra_args=None):
    """Real DishwasherMonitor with initialize() run for real (never stubbing the restore path -
    see test_dishwasher_restart_survival.py's module docstring for why). Copied from
    test_dishwasher_boot_announce_gate.py's make_app."""
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
        "programmes_file": REAL_PROGRAMMES_FILE,
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


def scheduled_callbacks_named(app, name):
    return [cb for cb, _d, _k in app.scheduled if getattr(cb, "__name__", "") == name]


def load_feedback_cycles(app):
    path = app.args["feedback_file"]
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f).get("cycles", [])


# ==========================================================================================
# FLAW 1 - boot re-applies the store payload the validator just REJECTED
# ==========================================================================================

class BootRejectedStoreDoesNotOverwriteHistoryResume(unittest.TestCase):
    """_maybe_resume_cycle_from_history() infers a real, live cycle from the plug's recorder
    history; the store's OWN Running candidate was separately rejected (20h-old start_time) a
    few lines earlier in the SAME boot. Before the fix, _restore_cycle_tracking_from_entity read
    the store's raw, unvalidated payload anyway (since it happened to also say "Running") and
    overwrote start_time/energy_start/detected_programme/expected_dur_at_start/
    notification_sent with the rejected values - reviving a resolved-Off cycle from 20h ago,
    turning a live 200-min-old wash into a bogus "finished 943 min ago" push and a ~1200-min
    junk feedback record."""

    def test_history_resumed_start_time_survives_a_rejected_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            inferred_start = now - timedelta(minutes=200)

            app, entities = make_boot_app(
                tmp,
                helper_state="Running",  # must NOT be Off/Unemptied/Emptied/Error, or history
                                          # resume's own helper check would refuse to even try.
                power_w="0",              # plug reads 0W right now - passive drying, not idle.
                energy_kwh="8.50",        # live energy_used = 8.50 - energy_start(from history)
                extra_args={**PRODUCTION_ARGS, "restore_history_window_hours": 5},
            )

            # Power history: idle before -260min, sustained high start at -200min (2 samples
            # within 300s satisfies find_first_sustained_high), tapering back to idle recently -
            # exactly the passive-dry-tail shape this file's own module docstring describes.
            app._history[POWER_SENSOR] = [
                {"state": "0", "last_changed": cs.format_utc(now - timedelta(minutes=260))},
                {"state": "1500", "last_changed": cs.format_utc(now - timedelta(minutes=200))},
                {"state": "1500", "last_changed": cs.format_utc(now - timedelta(minutes=199))},
                {"state": "1500", "last_changed": cs.format_utc(now - timedelta(minutes=100))},
                {"state": "0", "last_changed": cs.format_utc(now - timedelta(minutes=10))},
                {"state": "0", "last_changed": cs.format_utc(now - timedelta(minutes=1))},
            ]
            # Energy history: 0.50 kWh delta since before the inferred start - clears
            # min_energy_kwh (0.4) on its own, independent of the heater-burst count.
            app._history[ENERGY_SENSOR] = [
                {"state": "8.00", "last_changed": cs.format_utc(now - timedelta(minutes=260))},
                {"state": "8.50", "last_changed": cs.format_utc(now - timedelta(minutes=1))},
            ]

            # The on-disk store: a Running candidate old enough that boot resolution's own
            # staleness check rejects it outright (20h > max_running_hours=5h in PRODUCTION_ARGS).
            seed_store(tmp, {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(hours=20)),
                "energy_at_start": "7.00",
                "detected_programme": "eco",
                "expected_dur_at_start": 234,
                "max_power_w": 1800.0,
                "notification_sent": False,
            })

            app.initialize()

            self.assertLessEqual(
                abs((app.start_time - inferred_start).total_seconds()), 2,
                "start_time must be the history-inferred one, not the rejected store's 20h-old value",
            )
            self.assertEqual(app.state, "Running")
            self.assertEqual(app.sonos_calls, [])
            self.assertEqual(app.mobile_calls, [])
            self.assertEqual(load_feedback_cycles(app), [])


# ==========================================================================================
# FLAW 2 - expected_dur_at_start stuck at the 180-min "unknown" fallback anchors the boot
# self-heal to the wrong guard.
# ==========================================================================================

class BootSelfHealReclassifiesTheStaleGuardAnchor(unittest.TestCase):
    """expected_dur_at_start is stamped once at cycle start (often the 180-min UNKNOWN_FALLBACK,
    since the classifier still returns "unknown" for the first 10 minutes) and never refreshed
    for the rest of a live cycle. The store here restores detected_programme="eco" (trusted) but
    a live reading placed well outside eco's own 0.4-0.95 kWh rating (a boundary-drift case -
    the exact interaction the 2026-08-12 restored_detected_programme snapshot exists to guard
    against) makes a fresh reclassification say "gentle" (149 min guard) instead - so a fix that
    only re-stamps from a FRESH classification would still anchor to 149 (which doesn't raise
    the stale 180) and self-heal early. Anchoring to the RESTORED (trusted) programme's own
    guard instead must win."""

    def test_restart_at_210min_with_stale_180_anchor_does_not_finish_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            app, entities = make_boot_app(
                tmp,
                helper_state=None,
                power_w="0",
                energy_kwh="10.0",
                extra_args=PRODUCTION_ARGS,
            )
            seed_store(tmp, {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=210)),
                # 10.0 - 8.80 = 1.20 kWh used: outside eco's own 0.95 rating (boundary drift).
                "energy_at_start": "8.80",
                "detected_programme": "eco",
                "expected_dur_at_start": 180,  # stale "unknown" fallback, never refreshed
                "max_power_w": 1800.0,
                "notification_sent": False,
            })

            app.initialize()

            self.assertEqual(
                app.state, "Running",
                "must not finish before eco's real guard (234 * 0.95 = 222.3min) has elapsed",
            )
            self.assertEqual(app.sonos_calls, [])
            self.assertEqual(app.mobile_calls, [])
            self.assertFalse(app.notification_sent)
            self.assertIsNone(app.dry_tail_timer)
            self.assertIsNone(app._dry_tail_pending)
            self.assertEqual(load_feedback_cycles(app), [])


# ==========================================================================================
# FLAW 3 - _evaluate_pause_exit(force=True) never used force; unconditional skip_announce=False
# and unconditional feedback save regardless of whether the transition landed.
# ==========================================================================================

class PauseExitForceAndSkipAnnounce(unittest.TestCase):
    """Door open at 205 min with low power -> Paused (still short of the 234-min guard). Door
    closes 100s later; the pause-finish check fires 90s after that (stop_for) - only 190s since
    the pause began, well inside the 300s cooling period. Before the fix this was refused
    forever (no reconciler retries a refused Paused-exit) and STILL saved a phantom feedback
    record for a wash that never actually reached Unemptied."""

    def test_door_close_shortly_after_pause_reaches_unemptied_without_announce_or_duplicate(self):
        orig_profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES
        dm.DishwasherMonitor.PROGRAMME_PROFILES = {
            "eco": {"label": "ECO", "duration_min": 234, "duration_short_min": 74,
                     "max_energy_kwh": 0.95, "dry_tail_minutes": 0, "dry_tail_short_minutes": 0},
        }
        try:
            now = datetime(2026, 8, 12, 18, 0, 0, tzinfo=timezone.utc)
            app = make_live_app(now, start_minutes_ago=205, energy_now="1.5", energy_start=1.0)
            app.last_state_change = None

            with tempfile.TemporaryDirectory() as tmp:
                app.feedback_file = os.path.join(tmp, "dishwasher_feedback.json")

                app.states[app.door_sensor] = "on"
                app._handle_door_opened(app.states[app.state_entity])
                self.assertEqual(app.states[app.state_entity], "Paused")
                self.assertTrue(app.door_opened_during_cycle)

                app.now = app.now + timedelta(seconds=100)
                app.states[app.door_sensor] = "off"
                app._handle_door_closed(app.states[app.state_entity])

                finish_cbs = scheduled_callbacks_named(app, "_confirm_pause_finished")
                self.assertEqual(len(finish_cbs), 1, "expected the pause-finish check to be scheduled")

                app.now = app.now + timedelta(seconds=90)  # stop_for
                finish_cbs[0]({})

                self.assertEqual(
                    app.states[app.state_entity], "Unemptied",
                    "door-close pause-finish must not be refused forever by the cooling period",
                )
                self.assertEqual(
                    app.sonos_notifier.calls, [],
                    "the person who just opened the door must not be Sonos-blasted",
                )

                cycles = None
                if os.path.exists(app.feedback_file):
                    with open(app.feedback_file) as f:
                        cycles = json.load(f)["cycles"]
                self.assertEqual(len(cycles or []), 1)

                # A later re-open (to actually empty it) must not add a second record.
                app.states[app.door_sensor] = "on"
                app._handle_door_opened(app.states[app.state_entity])
                self.assertEqual(app.states[app.state_entity], "Emptied")

                with open(app.feedback_file) as f:
                    cycles_after = json.load(f)["cycles"]
                self.assertEqual(len(cycles_after), 1)
        finally:
            dm.DishwasherMonitor.PROGRAMME_PROFILES = orig_profiles


# ==========================================================================================
# FLAW 5 - classifier bands hardcoded vs YAML; in-code defaults dict also drifted from YAML.
# ==========================================================================================

def make_classify_app(now, *, elapsed_min, energy_used_kwh, power_w="0.0"):
    """Minimal object for _classify_programme() against the REAL dishwasher_programmes.yaml."""
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
    app.args = {"programmes_file": REAL_PROGRAMMES_FILE}
    app.log = lambda *a, **kw: None
    app._load_programme_profiles()

    app.power_sensor = "sensor.power"
    app.energy_sensor = "sensor.energy"
    app.short_entity = None
    app.energy_start = 10.0
    app.states = {app.power_sensor: power_w, app.energy_sensor: f"{10.0 + energy_used_kwh:.3f}"}
    app.get_state = lambda entity, **kw: app.states.get(entity)
    app.start_time = now - timedelta(minutes=elapsed_min)
    app._last_high_power_time = None
    app.now = now
    app._now_utc = lambda: app.now
    app.detected_short = False
    app.detected_quick_short = False
    return app


class ClassifierBandsDerivedFromProgrammeProfiles(unittest.TestCase):
    def setUp(self):
        self._orig = dm.DishwasherMonitor.PROGRAMME_PROFILES

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig

    def test_092kwh_at_145min_classifies_eco_not_gentle(self):
        """eco's real ceiling is 0.95 kWh, not the old hardcoded 0.9."""
        now = datetime.now(timezone.utc)
        app = make_classify_app(now, elapsed_min=145, energy_used_kwh=0.92)
        self.assertEqual(app._classify_programme(), "eco")

    def test_142kwh_does_not_classify_as_quick(self):
        """1.42 kWh sits inside Auto's real 1.45 kWh ceiling - the old hardcoded ">1.4 -> quick"
        catch-all forced a 58-min guard onto what could be a 108-min Auto cycle, finishing it
        ~34 min early."""
        now = datetime.now(timezone.utc)
        app = make_classify_app(now, elapsed_min=150, energy_used_kwh=1.42)
        self.assertNotEqual(app._classify_programme(), "quick")


class DefaultsDictMirrorsRealYaml(unittest.TestCase):
    def setUp(self):
        self._orig = dm.DishwasherMonitor.PROGRAMME_PROFILES

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig

    def test_defaults_fallback_matches_the_checked_in_yaml(self):
        app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
        app.args = {"programmes_file": "/nonexistent/dishwasher_programmes.yaml"}
        app.log = lambda *a, **kw: None

        app._load_programme_profiles()

        eco = dm.DishwasherMonitor.PROGRAMME_PROFILES["eco"]
        self.assertEqual(eco["duration_min"], 234)
        self.assertEqual(eco["max_energy_kwh"], 0.95)
        self.assertEqual(eco.get("dry_tail_minutes"), 35)


# ==========================================================================================
# FLAW 6 - Unemptied has no exit once a door-open edge is missed.
# ==========================================================================================

class UnemptiedPollReconciler(unittest.TestCase):
    """The unemptied watchdog is disabled by default (unemptied_timeout_hours: 0) and
    _poll_power used to do nothing for any state besides Running/Paused - a missed door-open
    edge (dropped listener, HA restart racing the event, ...) left the machine wedged in
    Unemptied forever."""

    def test_missed_door_edge_is_found_by_poll_and_transitions_to_emptied(self):
        now = datetime(2026, 8, 12, 20, 0, 0, tzinfo=timezone.utc)
        app = make_live_app(now, state="Unemptied", start_minutes_ago=250, power_w="0.0")
        app.state_since = now - timedelta(hours=2)
        app.poll_timer = None
        app._history[app.door_sensor] = [
            {"state": "on", "last_changed": cs.format_utc(now - timedelta(hours=1))},
        ]

        app._poll_power({})

        self.assertEqual(app.states[app.state_entity], "Emptied")

    def test_no_edge_keeps_polling_never_infers_emptied_from_power_alone(self):
        now = datetime(2026, 8, 12, 20, 0, 0, tzinfo=timezone.utc)
        app = make_live_app(now, state="Unemptied", start_minutes_ago=250, power_w="0.0")
        app.state_since = now - timedelta(hours=2)
        app.poll_timer = None
        # No door history at all - 0W alone must never be treated as "emptied".

        app._poll_power({})

        self.assertEqual(app.states[app.state_entity], "Unemptied")
        self.assertIsNotNone(
            app.poll_timer, "the poll loop must stay alive so a later edge can still be found"
        )
        self.assertEqual(len(scheduled_callbacks_named(app, "_poll_power")), 1)


class BootIntoUnemptiedArmsThePollReconciler(unittest.TestCase):
    """FLAW 6 (2026-09 audit, closed out): a boot resolving DIRECTLY into Unemptied - live
    entity or on-disk store, either source - used to arm no timer at all, so
    UnemptiedPollReconciler's fix above could only ever run after a LIVE
    _transition_to_unemptied call. A dishwasher that was ALREADY Unemptied before an AppDaemon
    restart got no reconciler chance whatsoever - and every deploy here IS an AppDaemon
    restart, so this was not an edge case, it was the common case."""

    def test_boot_into_unemptied_from_store_arms_poll_and_first_poll_finds_the_missed_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            state_since = now - timedelta(hours=2)
            app, entities = make_boot_app(tmp, helper_state=None, power_w="0")
            seed_store(tmp, {
                "state": "Unemptied",
                "state_since": cs.format_utc(state_since),
            })
            # The door was opened (and the listener missed it, or AppDaemon was down for it)
            # an hour ago - after state_since, before this restart.
            app._history[DOOR_SENSOR] = [
                {"state": "on", "last_changed": cs.format_utc(now - timedelta(hours=1))},
            ]

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            poll_cbs = scheduled_callbacks_named(app, "_poll_power")
            self.assertEqual(
                len(poll_cbs), 1,
                "boot resolving into Unemptied must arm the same poll timer Running/Paused get",
            )

            poll_cbs[0]({})

            self.assertEqual(app.state, "Emptied")


# ==========================================================================================
# FLAW 7 - learning-store key mismatch between save / reload / remove.
# ==========================================================================================

def make_feedback_app(feedback_file):
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
    app.feedback_file = feedback_file
    app._learned_durations = {}
    app.short_entity = None
    app.detected_short = False
    app.detected_quick_short = False
    app.log = lambda *a, **kw: None
    app.get_state = lambda entity, **kw: None
    app._now_utc = lambda: datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
    return app


class LearnedDurationKeyConsistency(unittest.TestCase):
    """_save_cycle_feedback used to credit _learned_durations[confirmed] directly, but
    _load_and_apply_feedback keys a short=True eco/quick record as eco_short/quick_short - the
    in-memory key right after a save and the key after a reload diverged. Removal also
    decremented without checking programme_confirmed_by_human."""

    def test_short_eco_record_uses_the_same_key_before_and_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
            app = make_feedback_app(feedback_file)
            app.detected_short = True  # ECO short=Yes -> _eco_short_active() -> True

            app._save_cycle_feedback(
                predicted="eco", confirmed="eco", duration_min=80.0, energy_kwh=0.45,
                max_power_w=1500, programme_confirmed_by_human=True,
            )
            self.assertEqual(len(app._learned_durations), 1)
            key_after_save = next(iter(app._learned_durations))

            reloaded = make_feedback_app(feedback_file)
            reloaded._load_and_apply_feedback()
            self.assertEqual(len(reloaded._learned_durations), 1)
            key_after_reload = next(iter(reloaded._learned_durations))

            self.assertEqual(
                key_after_save, key_after_reload,
                f"in-memory key {key_after_save!r} must match the key {key_after_reload!r} the "
                f"SAME record keys to after a reload",
            )
            self.assertEqual(key_after_save, "eco_short")

    def test_removing_an_unconfirmed_record_leaves_the_learned_average_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
            app = make_feedback_app(feedback_file)

            app._save_cycle_feedback(
                predicted="eco", confirmed="eco", duration_min=200.0, energy_kwh=0.6,
                max_power_w=1500, programme_confirmed_by_human=True,
            )
            self.assertEqual(app._learned_durations.get("eco"), {"n": 1, "avg": 200.0})

            # An UNCONFIRMED record for the same programme - never counted toward the average.
            app._save_cycle_feedback(
                predicted="eco", confirmed="eco", duration_min=999.0, energy_kwh=0.9,
                max_power_w=1500, programme_confirmed_by_human=False,
            )
            self.assertEqual(app._learned_durations.get("eco"), {"n": 1, "avg": 200.0})

            app._remove_last_cycle_feedback()  # pops the UNCONFIRMED 999.0 record

            self.assertEqual(
                app._learned_durations.get("eco"), {"n": 1, "avg": 200.0},
                "removing a record that was never confirmed must not touch the learned average",
            )


# ==========================================================================================
# FLAW 4 - restart inside the dry tail feeds the elapsed tail into the learned duration.
# (mirrors test_dishwasher_dry_tail.py::FeedbackDoesNotAbsorbTheTail for the restart path)
# ==========================================================================================

class RestartInsideDryTailDoesNotAbsorbTheTail(unittest.TestCase):
    """eco: nominal 234 min, finish_guard_fraction 0.95 -> guard opens at 222.3min; dry_tail
    35min -> machine's own end at 257.3min. Restarting 15 min into that tail (at 237.3min
    elapsed) must record the SAME duration (222.3min, guard-open) a normal, no-restart finish
    would have - not the tail-inflated wall-clock value _correct_duration cannot rescue (no more
    heating history to anchor to past the last real burst)."""

    def test_restart_15min_into_the_tail_records_the_guard_open_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            guard_open_min = 234 * 0.95  # 222.3
            restart_elapsed_min = guard_open_min + 15  # 15 min into the 35-min tail

            app, entities = make_boot_app(
                tmp,
                helper_state=None,
                power_w="0",
                energy_kwh="10.0",
                extra_args=PRODUCTION_ARGS,
            )
            seed_store(tmp, {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=restart_elapsed_min)),
                "energy_at_start": "9.15",  # 10.0 - 9.15 = 0.85 kWh - within eco's real band
                "detected_programme": "eco",
                "expected_dur_at_start": 234,
                "max_power_w": 1800.0,
                "last_high_power_time": cs.format_utc(
                    now - timedelta(minutes=restart_elapsed_min - 150)
                ),
                "notification_sent": False,
            })

            app.initialize()

            # Guard has opened but the 35-min tail has not fully elapsed - deferred, not immediate.
            self.assertEqual(app.state, "Running")
            tail_cbs = scheduled_callbacks_named(app, "_dry_tail_elapsed")
            self.assertEqual(len(tail_cbs), 1, "expected a single deferred dry-tail transition")
            self.assertIsNotNone(app._dry_tail_pending)
            self.assertAlmostEqual(
                app._dry_tail_pending["run_minutes"], guard_open_min, delta=1.0,
                msg="the STASHED run_minutes must already be capped at guard-open, before the tail elapses",
            )

            tail_cbs[0]({})

            self.assertEqual(app.state, "Unemptied")
            cycles = load_feedback_cycles(app)
            self.assertEqual(len(cycles), 1)
            self.assertAlmostEqual(
                cycles[-1]["duration_min"], guard_open_min, delta=1.0,
                msg="recorded duration must match the no-restart (guard-open) baseline, not the "
                    "wall-clock time at restart",
            )


# ==========================================================================================
# Harness 2: live (post-boot) methods - flaws 3 and 6
# ==========================================================================================

class FakeSonos:
    def __init__(self):
        self.calls = []

    def notify(self, message):
        self.calls.append(message)


def make_live_app(now, *, state="Running", start_minutes_ago=205, energy_now="1.5",
                   energy_start=1.0, power_w="0.0", door_state="off"):
    """Minimal DishwasherMonitor for exercising live (post-boot) methods directly - copied from
    test_dishwasher_dry_tail.py's make_running_app."""
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)

    app.power_sensor = POWER_SENSOR
    app.energy_sensor = ENERGY_SENSOR
    app.door_sensor = DOOR_SENSOR
    app.state_entity = STATE_ENTITY
    app.ui_state_select = None
    app.confirmed_programme_entity = None
    app.short_entity = None

    app.start_w = 8.0
    app.stop_w = 2.0
    app.stop_for = 90
    app.min_cycle_minutes = 74
    app.min_energy_kwh = 0.4
    app.fill_window_minutes = 74
    app.pause_timeout_minutes = 5
    app.cooling_period = 300
    app.max_running_hours = 5
    app.unemptied_timeout_hours = 0
    app.emptied_timeout_minutes = 30
    app.min_emptying_seconds = 45
    app.energy_active_watts = 100.0
    app.high_power_threshold = 2
    app.low_power_threshold = 5
    app.pattern_window = 10
    app.start_sustain_seconds_without_door = 120
    app.door_close_fast_start_window_s = 900
    app.finish_guard_fraction = 0.95
    app.finish_guard_use_learned = False
    app.finish_guard_min_learned_n = 5
    app._learned_durations = {}

    app.state = state
    app.states = {
        app.state_entity: state,
        app.power_sensor: power_w,
        app.energy_sensor: energy_now,
        app.door_sensor: door_state,
    }
    app.attrs = {}
    app.start_time = now - timedelta(minutes=start_minutes_ago)
    app.energy_start = energy_start
    app.expected_dur_at_start = None
    app.last_state_change = now - timedelta(minutes=start_minutes_ago)
    app.state_since = app.last_state_change
    app.notification_sent = False
    app.detected_programme = "unknown"
    app.detected_short = False
    app.detected_quick_short = False
    app.max_power_w = 1800.0
    app._last_high_power_time = now - timedelta(minutes=start_minutes_ago)
    app.power_readings = []
    app.high_power_counter = 0
    app.low_power_counter = 0
    app._sustain_start_begin = None
    app._strict_start_until_door_or_sustain = False
    app._start_candidate_active = False
    app._start_candidate_source = None
    app._start_candidate_active_samples = 0
    app._start_candidate_max_w = 0.0
    app._start_candidate_began_at = None
    app._start_candidate_energy_at_start = None
    app._start_candidate_window_timer = None
    app._start_candidate_idle_timer = None
    app.pending_start_used_fast_path = False
    app.door_fast_start_armed_until = None
    app.last_door_closed_at = None
    app.door_opened_time = None
    app.door_opened_during_cycle = False
    app.pause_from_low_power = False
    app.program_timer = None
    app.poll_timer = None
    app.classify_timer = None
    app.low_power_timer = None
    app.pause_timer = None
    app.pause_finish_timer = None
    app.running_watchdog_timer = None
    app.unemptied_watchdog_timer = None
    app.emptied_timeout_timer = None
    app.emptied_at = None
    app.power_unavailable_error_timer = None
    app.dry_tail_timer = None
    app._dry_tail_pending = None
    app.notify_target = ["mikkel"]
    app._plug_error_pushed = False

    app.sonos_notifier = FakeSonos()
    app.feedback_file = "/nonexistent/dishwasher_feedback_test.json"

    app.now = now
    app._now_utc = lambda: app.now

    app._history = {}

    def get_state(entity, **kw):
        attr = kw.get("attribute")
        if attr == "all":
            return {"attributes": dict(app.attrs.get(entity, {}))}
        if attr:
            return app.attrs.get(entity, {}).get(attr)
        return app.states.get(entity)

    app.get_state = get_state

    def get_history(entity_id=None, start_time=None, end_time=None, **kw):
        return [list(app._history.get(entity_id, []))]

    app.get_history = get_history

    app.set_state_calls = []

    def set_state(entity, **kw):
        app.set_state_calls.append({"entity": entity, **kw})
        if "state" in kw:
            app.states[entity] = kw["state"]
        if "attributes" in kw:
            if kw.get("replace"):
                app.attrs[entity] = dict(kw["attributes"])
            else:
                app.attrs.setdefault(entity, {}).update(kw["attributes"])

    app.set_state = set_state

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.call_service = lambda *a, **kw: None
    app.get_app = lambda name: None
    app.create_task = lambda coro: None

    app.scheduled = []
    app.canceled_timers = []
    app.timer_running = lambda handle: True
    app.cancel_timer = lambda handle: app.canceled_timers.append(handle)

    def run_in(cb, delay, **kw):
        idx = len(app.scheduled)
        handle = f"timer#{idx}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    return app


if __name__ == "__main__":
    unittest.main()
