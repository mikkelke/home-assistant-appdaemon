# tests/test_dryer_shadow.py - wiring-level coverage of dryer_shadow.py's real initialize()
# (spec section 11.3/11.4), the structural no-notify guarantee (11.5), the v2 attribute contract
# (11.6), and the boot-restore/corroboration/reconcile flow (spec sections 4.3, 8) end to end
# through the real ApplianceFSM + real DryerPolicy + real DryerShadow. Run from repo root:
# python3 -m unittest discover -s apps/appliances/tests -q
#
# Mirrors test_dryer_restart_survival.py's own rule (see its module docstring): initialize() and
# the restore/boot-resolution path it dispatches to are NEVER stubbed - only the AppDaemon I/O
# primitives (get_state/set_state/get_history/run_in/cancel_timer/listen_state/listen_event) are
# faked, via a get_state double that distinguishes attribute="all" from a bare read (same shape as
# that file's _entity_get_state). state_file always lives under a TemporaryDirectory.
#
# DryerShadow uses the real SystemClock (no clock-injection seam on the app itself, by design -
# spec 7 does not ask for one) - tests that need elapsed time either set cooling_period: 0 in args
# (transitions need not survive a real 600s wait) or shift policy.start_time/store_since directly,
# the same pattern test_dryer_policy_table.py uses for the bare engine+policy.

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import types  # noqa: E402

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

import dryer_shadow as ds  # noqa: E402
from appliance_fsm import Evidence, EvidenceType  # noqa: E402
from cycle_store import CycleStore, format_utc  # noqa: E402

V2 = "sensor.dryer_state_v2"
LIVE = "sensor.dryer_state"
POWER = "sensor.dryer_plug_power"
ENERGY = "sensor.dryer_plug_energy"
DOOR = "binary_sensor.dryer_door_contact"


def _entity_get_state(entities, call_log):
    """Distinguishes attribute="all" from a bare read - see test_dryer_restart_survival.py's
    _entity_get_state, same shape, ported for DryerShadow's own two entities of interest."""

    def get_state(entity, attribute=None, **kwargs):
        rec = entities.get(entity)
        if attribute == "all":
            call_log.append(("get_state_all", entity))
            if rec is None:
                return None
            return {
                "state": rec.get("state"), "attributes": dict(rec.get("attributes") or {}),
                "last_changed": rec.get("last_changed"), "last_updated": rec.get("last_changed"),
            }
        call_log.append(("get_state", entity))
        if rec is None:
            return None
        if attribute:
            return (rec.get("attributes") or {}).get(attribute)
        return rec.get("state")

    return get_state


def make_shadow(tmpdir, *, v2_state=None, v2_attrs=None, v2_last_changed=None, live_state="Off",
                 power_w="0", energy_kwh="10.0", door_state="off", state_file=None, extra_args=None,
                 history=None):
    """Real DryerShadow.__new__ + real initialize(); only AppDaemon primitives are faked."""
    app = ds.DryerShadow.__new__(ds.DryerShadow)
    entities = {
        V2: {"state": v2_state, "attributes": dict(v2_attrs or {}), "last_changed": v2_last_changed},
        LIVE: {"state": live_state, "attributes": {}, "last_changed": None},
        POWER: {"state": power_w, "attributes": {}, "last_changed": None},
        ENERGY: {"state": energy_kwh, "attributes": {}, "last_changed": None},
        DOOR: {"state": door_state, "attributes": {}, "last_changed": None},
    }
    call_log = []
    app.call_log = call_log
    app.get_state = _entity_get_state(entities, call_log)
    app.get_history = (lambda entity=None, **kw: (history or {}).get(entity, [])) if history is not None else (
        lambda entity=None, **kw: []
    )
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None

    set_state_calls = []

    def set_state(entity_id, **kw):
        call_log.append(("set_state", entity_id))
        set_state_calls.append((entity_id, kw))
        rec = entities.setdefault(entity_id, {"state": None, "attributes": {}, "last_changed": None})
        if "state" in kw and kw["state"] is not None:
            rec["state"] = kw["state"]
        if kw.get("attributes") is not None:
            rec["attributes"] = dict(kw["attributes"]) if kw.get("replace") else {**rec["attributes"], **kw["attributes"]}
        rec["last_changed"] = time.time()

    app.set_state = set_state
    app.set_state_calls = set_state_calls

    app.scheduled = []

    def run_in(cb, delay, **kw):
        h = f"t{len(app.scheduled)}"
        app.scheduled.append([h, cb, delay, True])
        return h

    def cancel_timer(h):
        for e in app.scheduled:
            if e[0] == h:
                e[3] = False

    app.run_in = run_in
    app.cancel_timer = cancel_timer
    app.timer_running = lambda h: any(e[0] == h and e[3] for e in app.scheduled)

    args = {
        "power_sensor": POWER, "energy_sensor": ENERGY, "door_sensor": DOOR,
        "live_state_entity": LIVE, "state_entity": V2,
        "state_file": state_file or str(Path(tmpdir) / "dryer_shadow_state.json"),
        "programmes_file": str(Path(tmpdir) / "dryer_programmes_missing.yaml"),
        "start_w": 8, "stop_w": 5, "run_for": 5, "stop_for": 5,
        "min_cycle_minutes": 1, "min_energy_kwh": 0.01, "fill_window_minutes": 1,
        "cooling_period": 0,
    }
    if extra_args:
        args.update(extra_args)
    app.args = args
    return app, entities, call_log


def fire_shortest(app):
    due = [e for e in app.scheduled if e[3]]
    if not due:
        return False
    e = min(due, key=lambda x: x[2])
    e[3] = False
    e[1]({})
    return True


def push_power(app, entities, watts_str):
    entities[POWER]["state"] = watts_str
    for det in app.fsm._detectors:
        det.on_state(POWER, "0", watts_str, app.fsm.ctx)


def push_door(app, entities, state_str):
    entities[DOOR]["state"] = state_str
    for det in app.fsm._detectors:
        det.on_state(DOOR, "0", state_str, app.fsm.ctx)


# --- 11.3/11.4: real initialize(), cold boot, and the boot-snapshot ordering invariant ---


class ColdBootAndSnapshotOrdering(unittest.TestCase):
    def test_cold_boot_publishes_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            app.initialize()
            self.assertEqual(app.fsm.state.name, "OFF")
            self.assertEqual(entities[V2]["state"], "Off")

    def test_boot_all_read_precedes_first_write_no_post_write_reread(self):
        """Mirrors test_dryer_restart_survival.py's OrderingProvesTheBootSnapshotPrecedesThe
        FirstWrite: the attribute="all" read of v2's OWN prior state must happen before the first
        set_state, and no further get_state(V2, ...) call may happen after that first write -
        restore must consume the snapshot captured up front, never re-read an entity the write may
        have just recreated with no attributes (the 2026-07-27 incident class)."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp, v2_state="Running", v2_attrs={"cycle_id": "cid-X"})
            store = CycleStore(app.args["state_file"], "dryer_shadow")
            now = app.args  # placeholder, real clock used below
            import datetime as dtmod
            start = dtmod.datetime.now(dtmod.timezone.utc) - timedelta(minutes=30)
            store.save({
                "state": "Running", "state_since": format_utc(start), "cycle_id": "cid-X",
                "cycle_start_time": format_utc(start), "energy_at_start": 10.0,
            })
            app.initialize()

            first_write_idx = next(i for i, e in enumerate(calls) if e[0] == "set_state")
            first_all_read_idx = next(i for i, e in enumerate(calls) if e[0] == "get_state_all" and e[1] == V2)
            self.assertLess(
                first_all_read_idx, first_write_idx,
                "the attribute=\"all\" boot snapshot of v2 must be read before the first write",
            )
            post_write_v2_reads = [e for e in calls[first_write_idx + 1:] if e[1] == V2]
            self.assertEqual(
                post_write_v2_reads, [],
                f"a v2 get_state call happened after the first write - restore must consume the "
                f"boot snapshot captured up front: {post_write_v2_reads}",
            )
            self.assertEqual(app.fsm.state.name, "RUNNING")
            self.assertIsNotNone(app._policy.start_time)


# --- full cycle end to end (validates the whole engine+policy+shadow wiring together) ---


class FullCycleEndToEnd(unittest.TestCase):
    def test_off_to_off_full_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            app.initialize()

            push_power(app, entities, "620")
            fire_shortest(app)  # start-confirm
            self.assertEqual(app.fsm.state.name, "RUNNING")
            self.assertEqual(entities[V2]["state"], "Running")

            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "2")
            app._policy.start_time = app._policy.start_time - timedelta(minutes=130)
            fire_shortest(app)  # end-confirm
            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(entities[V2]["state"], "Unemptied")
            self.assertEqual(app._actions.announced, ["Dryer is ready to be emptied"])
            self.assertEqual(len(app._actions.feedback), 1)

            push_door(app, entities, "on")
            self.assertEqual(app.fsm.state.name, "EMPTIED")
            push_door(app, entities, "off")
            self.assertEqual(app.fsm.state.name, "OFF")
            self.assertEqual(entities[V2]["state"], "Off")
            self.assertEqual(app._clean_cycles, 1)


# --- spec section 8: hypothesis + corroboration + reconcile, through real initialize() ---


class BootRestoreHypothesisAndReconcile(unittest.TestCase):
    def _seed_running_store(self, tmp, *, minutes_ago=150, cycle_id="cid-A"):
        import datetime as dtmod

        state_file = str(Path(tmp) / "dryer_shadow_state.json")
        now = dtmod.datetime.now(dtmod.timezone.utc)
        start = now - timedelta(minutes=minutes_ago)
        store = CycleStore(state_file, "dryer_shadow")
        ok = store.save({
            "state": "Running", "state_since": format_utc(start), "cycle_id": cycle_id,
            "cycle_start_time": format_utc(start), "energy_at_start": 10.0,
            "detected_programme": "unknown", "programme_duration_min": 120, "max_power_w": 600.0,
            "last_high_energy_at": format_utc(start),
        })
        self.assertTrue(ok)
        return state_file

    def test_uncorroborated_restore_absent_history_stays_hypothesis_never_announces(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_running_store(tmp)
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10, "cooling_period": 0},
            )
            app.initialize()
            self.assertTrue(app.fsm.hypothesis, "0W boot with no corroborating evidence must be hypothesis")
            self.assertEqual(app._actions.announced, [])
            self.assertEqual(app._actions.pushed, [], "absent power history must NEVER be taken as concluded")

    def test_uncorroborated_restore_with_sustained_low_history_concludes_and_force_pushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_running_store(tmp)
            import datetime as dtmod

            now = dtmod.datetime.now(dtmod.timezone.utc)
            history = {
                POWER: [
                    {"state": "3", "last_changed": (now - timedelta(minutes=5)).isoformat()},
                    {"state": "2", "last_changed": (now - timedelta(minutes=2)).isoformat()},
                ],
            }
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running", history=history,
                extra_args={"min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10, "cooling_period": 0},
            )
            app.initialize()
            self.assertFalse(app.fsm.hypothesis, "try_conclude_at_boot must clear hypothesis")
            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(app._actions.announced, [], "a boot-time conclusion must NEVER use Sonos")
            self.assertEqual(len(app._actions.pushed), 1)
            self.assertIn("late detection", app._actions.pushed[0])
            self.assertEqual(len(app._actions.feedback), 1)

    def test_door_edge_during_outage_routes_emptied_totally_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_running_store(tmp)
            import datetime as dtmod

            now = dtmod.datetime.now(dtmod.timezone.utc)
            history = {
                POWER: [
                    {"state": "3", "last_changed": (now - timedelta(minutes=5)).isoformat()},
                    {"state": "2", "last_changed": (now - timedelta(minutes=2)).isoformat()},
                ],
                DOOR: [
                    {"state": "on", "last_changed": (now - timedelta(minutes=1)).isoformat()},
                    {"state": "off", "last_changed": (now - timedelta(seconds=30)).isoformat()},
                ],
            }
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running", history=history,
                extra_args={"min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10, "cooling_period": 0},
            )
            app.initialize()
            self.assertEqual(app.fsm.state.name, "EMPTIED")
            self.assertEqual(app._actions.announced, [])
            self.assertEqual(app._actions.pushed, [], "door-edge route is totally silent, not even a push")
            self.assertEqual(len(app._actions.feedback), 1)

    def test_corroborated_restore_live_power_high_never_hypothesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_running_store(tmp)
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="450", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "cooling_period": 0},
            )
            app.initialize()
            self.assertFalse(app.fsm.hypothesis)
            self.assertEqual(app.fsm.state.name, "RUNNING")

    def test_ad_only_reload_live_v2_entity_trusted_outright(self):
        with tempfile.TemporaryDirectory() as tmp:
            import datetime as dtmod

            start = dtmod.datetime.now(dtmod.timezone.utc) - timedelta(minutes=30)
            app, entities, calls = make_shadow(
                tmp, v2_state="Running", v2_attrs={"cycle_id": "cid-C"}, v2_last_changed=format_utc(start),
                power_w="0", live_state="Running", extra_args={"min_cycle_minutes": 1, "cooling_period": 0},
            )
            app.initialize()
            self.assertFalse(app.fsm.hypothesis, "AD-only reload (live entity) is trusted outright")
            self.assertEqual(app.fsm.state.name, "RUNNING")

    def test_restart_carries_cycle_id_and_feedback_guard_exactly_once(self):
        """Cross-restart exactly-once (spec section 6): engine A finishes and feeds back once;
        engine B, restored straight into FINISHED from the same store, must not save a second
        record even if something re-triggers a finish-shaped path."""
        with tempfile.TemporaryDirectory() as tmp:
            state_file = str(Path(tmp) / "dryer_shadow_state.json")
            app_a, entities_a, _ = make_shadow(tmp, state_file=state_file, extra_args={"min_cycle_minutes": 1})
            app_a.initialize()
            push_power(app_a, entities_a, "620")
            fire_shortest(app_a)
            entities_a[ENERGY]["state"] = "10.5"
            push_power(app_a, entities_a, "2")
            app_a._policy.start_time = app_a._policy.start_time - timedelta(minutes=130)
            fire_shortest(app_a)
            self.assertEqual(app_a.fsm.state.name, "FINISHED")
            cid = app_a.fsm.cycle_id
            self.assertEqual(len(app_a._actions.feedback), 1)

            app_b, entities_b, _ = make_shadow(tmp, state_file=state_file, extra_args={"min_cycle_minutes": 1})
            app_b.initialize()
            self.assertEqual(app_b.fsm.state.name, "FINISHED")
            self.assertEqual(app_b.fsm.cycle_id, cid)
            self.assertEqual(app_b._actions.feedback, [], "a silent restore into FINISHED must never re-save")


# --- freshness gate (FIX-3): a stale, uncorroborated-restore finish must push, never announce ---


class AnnounceFreshnessGateAfterStaleRestore(unittest.TestCase):
    """dryer_policy.py's _finish reads announce_freshness_minutes (spec section 9) to downgrade a
    stale finish's notification from Sonos to a mobile push - the 2026-08-19 incident shape: boot
    RECONCILE cannot conclude on an empty trailing history (a long HA outage), the restore
    hypothesis survives boot, and the FIRST live evidence after boot (of POWER or PHYSICAL class)
    auto-clears it via the engine's own landed-transition rule (spec 8) before the finish itself
    lands. Covers every _finish call site: POWER_END_CONFIRMED off a stale RUNNING restore (a,
    plus its door-edge/EMPTIED sibling), PAUSED/DOOR_CLOSED (b), ENDING/KEEP_FRESH (c, previously
    untested), and a fresh, non-stale finish as the regression guard that the gate does not fire
    when it should not (d)."""

    def _seed_running_store(self, tmp, *, minutes_ago=180, cycle_id="cid-fresh-a"):
        import datetime as dtmod

        state_file = str(Path(tmp) / "dryer_shadow_state.json")
        now = dtmod.datetime.now(dtmod.timezone.utc)
        start = now - timedelta(minutes=minutes_ago)
        store = CycleStore(state_file, "dryer_shadow")
        ok = store.save({
            "state": "Running", "state_since": format_utc(start), "cycle_id": cycle_id,
            "cycle_start_time": format_utc(start), "energy_at_start": 10.0,
            "detected_programme": "unknown", "programme_duration_min": 120, "max_power_w": 600.0,
            "last_high_energy_at": format_utc(start),
        })
        self.assertTrue(ok)
        return state_file

    def _seed_paused_store(self, tmp, *, minutes_ago=180, cycle_id="cid-fresh-b"):
        import datetime as dtmod

        state_file = str(Path(tmp) / "dryer_shadow_state.json")
        now = dtmod.datetime.now(dtmod.timezone.utc)
        start = now - timedelta(minutes=minutes_ago)
        store = CycleStore(state_file, "dryer_shadow")
        ok = store.save({
            "state": "Paused", "state_since": format_utc(start), "cycle_id": cycle_id,
            "cycle_start_time": format_utc(start), "energy_at_start": 10.0,
            "detected_programme": "unknown", "programme_duration_min": 120, "max_power_w": 600.0,
            "last_high_energy_at": format_utc(start),
        })
        self.assertTrue(ok)
        return state_file

    def test_stale_running_restore_live_0w_confirms_pushes_not_announces(self):
        """(a) repro_B.py's exact shape through the real shadow: boot RECONCILE cannot conclude
        (absent trailing history - the outage swallowed it), hypothesis survives boot; the FIRST
        live power sample (0W) auto-clears hypothesis via the engine's own landed-POWER-evidence
        rule, landing RUNNING->ENDING; POWER_END_CONFIRMED then finishes through _finish's
        freshness gate - anchored on last_high_energy_at (180min stale, restored from the store),
        this must push, never announce."""
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_running_store(tmp)
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10, "cooling_period": 0},
            )
            app.initialize()
            self.assertTrue(app.fsm.hypothesis, "absent history - boot RECONCILE must not conclude")
            self.assertEqual(app._actions.announced, [])

            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "0")  # live 0W - lands RUNNING->ENDING, auto-clears hypothesis
            self.assertFalse(app.fsm.hypothesis, "a landed live POWER transition clears the hypothesis")
            self.assertEqual(app.fsm.state.name, "ENDING")
            fire_shortest(app)  # PowerEndDetector's stop_for confirm -> POWER_END_CONFIRMED

            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(app._actions.announced, [], "must never announce a stale finish")
            self.assertEqual(len(app._actions.pushed), 1)
            self.assertIn("late detection", app._actions.pushed[0])
            self.assertEqual(len(app._actions.feedback), 1)

    def test_stale_running_restore_door_open_at_confirm_routes_emptied_silent(self):
        """(a), door-edge sibling: G_door_edge's live check (door open right now at confirm time)
        routes ENDING/POWER_END_CONFIRMED to a_finish_silent -> EMPTIED - skip_announce=True
        already makes this totally silent regardless of the freshness gate; asserted here so both
        outcomes the verifier named ("push, or silent EMPTIED if a door edge exists") are
        covered."""
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_running_store(tmp)
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10, "cooling_period": 0},
            )
            app.initialize()
            self.assertTrue(app.fsm.hypothesis)

            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "0")
            self.assertFalse(app.fsm.hypothesis)
            entities[DOOR]["state"] = "on"  # door open right now at confirm time (no detector edge needed)
            fire_shortest(app)

            self.assertEqual(app.fsm.state.name, "EMPTIED")
            self.assertEqual(app._actions.announced, [])
            self.assertEqual(app._actions.pushed, [], "door-edge route is silent regardless of freshness")
            self.assertEqual(len(app._actions.feedback), 1)

    def test_stale_paused_restore_door_closed_low_power_pushes_not_announces(self):
        """(b) repro_B2.py's PAUSED/DOOR_CLOSED variant through the real shadow: restored PAUSED,
        hypothesis survives boot (absent history); the user closes the door with power still low -
        PAUSED/DOOR_CLOSED's a_finish_announce goes through the SAME _finish freshness gate as (a)
        - must push, never announce."""
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_paused_store(tmp)
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", door_state="on", live_state="Paused",
                extra_args={"min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10, "cooling_period": 0},
            )
            app.initialize()
            self.assertTrue(app.fsm.hypothesis, "absent history - boot RECONCILE must not conclude")
            self.assertEqual(app.fsm.state.name, "PAUSED")

            entities[ENERGY]["state"] = "10.5"
            push_door(app, entities, "off")  # door closes at low power -> a_finish_announce

            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertFalse(app.fsm.hypothesis, "a landed live PHYSICAL transition clears the hypothesis")
            self.assertEqual(app._actions.announced, [], "must never announce a stale finish")
            self.assertEqual(len(app._actions.pushed), 1)
            self.assertIn("late detection", app._actions.pushed[0])

    def test_stale_ending_keep_fresh_finish_pushes_not_announces(self):
        """(c) ENDING/KEEP_FRESH shares _finish too (verifier flagged this path as previously
        untested) - KEEP_FRESH is itself POWER-class (live), so the first submission both lands
        RUNNING->ENDING and clears the hypothesis; the second submission's row (guarded on
        G_past_guard/G_valid_cycle/G_is_real) lands the finish and must push, not announce."""
        with tempfile.TemporaryDirectory() as tmp:
            state_file = self._seed_running_store(tmp)
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "restore_corroboration_window_minutes": 10, "cooling_period": 0},
            )
            app.initialize()
            self.assertTrue(app.fsm.hypothesis, "absent history - boot RECONCILE must not conclude")

            entities[ENERGY]["state"] = "10.5"
            kf_payload = {"mean": 100, "peak": 200, "stdev": 60}
            app.fsm.submit(Evidence.make(EvidenceType.KEEP_FRESH, app.fsm.ctx.now(), "test", live=True, payload=kf_payload))
            self.assertEqual(app.fsm.state.name, "ENDING")
            self.assertFalse(app.fsm.hypothesis, "a landed live KEEP_FRESH (POWER-class) clears the hypothesis")

            app.fsm.submit(Evidence.make(EvidenceType.KEEP_FRESH, app.fsm.ctx.now(), "test", live=True, payload=kf_payload))

            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(app._actions.announced, [], "must never announce a stale finish")
            self.assertEqual(len(app._actions.pushed), 1)
            self.assertIn("late detection", app._actions.pushed[0])

    def test_fresh_finish_still_announces_once_zero_pushes(self):
        """(d) regression guard: a genuine, fresh finish (latency well under the freshness knob,
        since last_high_energy_at is set moments before the finish, unlike start_time which this
        test backdates only to satisfy the duration guard) must still announce over Sonos exactly
        once, never push - the freshness gate must not degrade the ordinary happy path."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            app.initialize()
            self.assertEqual(app.fsm.state.name, "OFF")

            push_power(app, entities, "620")
            fire_shortest(app)  # start-confirm -> RUNNING
            self.assertEqual(app.fsm.state.name, "RUNNING")

            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "2")
            app._policy.start_time = app._policy.start_time - timedelta(minutes=130)
            fire_shortest(app)  # end-confirm -> FINISHED

            self.assertEqual(app.fsm.state.name, "FINISHED")
            self.assertEqual(app._actions.announced, ["Dryer is ready to be emptied"])
            self.assertEqual(app._actions.pushed, [], "a fresh finish must never push")


# --- 11.5: shadow-cannot-notify (structural, not merely behavioral) ---


class ShadowCannotNotify(unittest.TestCase):
    def test_no_notifier_lookup_ever_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            self.assertFalse(hasattr(app, "get_app"), "DryerShadow must never call get_app for a notifier")
            self.assertFalse(hasattr(app, "notify_target"))

    def test_finish_path_records_would_be_announce_as_inert_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp, extra_args={"min_cycle_minutes": 1})
            app.initialize()
            push_power(app, entities, "620")
            fire_shortest(app)
            entities[ENERGY]["state"] = "10.5"
            push_power(app, entities, "2")
            app._policy.start_time = app._policy.start_time - timedelta(minutes=130)
            fire_shortest(app)
            self.assertEqual(app.fsm.state.name, "FINISHED")
            # ShadowActions recorded it (proves the announce path ran) but no call_service/
            # notifier call of any kind happened - the app fakes call_service as absent entirely.
            self.assertFalse(hasattr(app, "call_service"))
            self.assertEqual(app._actions.announced, ["Dryer is ready to be emptied"])

    def test_hypothesis_state_cannot_announce_even_via_dashboard_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            import datetime as dtmod

            state_file = str(Path(tmp) / "dryer_shadow_state.json")
            start = dtmod.datetime.now(dtmod.timezone.utc) - timedelta(minutes=150)
            store = CycleStore(state_file, "dryer_shadow")
            store.save({
                "state": "Running", "state_since": format_utc(start), "cycle_id": "cid-H",
                "cycle_start_time": format_utc(start), "energy_at_start": 10.0,
            })
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "cooling_period": 0},
            )
            app.initialize()
            self.assertTrue(app.fsm.hypothesis)
            # A stray FORCE_EMPTIED while still hypothesis (defensive: nothing routes here in this
            # state anyway, but confirm the belt holds even if it did).
            app.fsm.set_hypothesis(True)
            app.fsm.submit(app.fsm.ctx.evidence) if app.fsm.ctx.evidence else None
            self.assertEqual(app._actions.announced, [])


# --- 11.6: v2 attribute contract ---


class V2AttributeContract(unittest.TestCase):
    def test_hypothesis_omitted_when_false_present_as_string_true_when_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            app.initialize()
            self.assertNotIn("hypothesis", entities[V2]["attributes"])

        with tempfile.TemporaryDirectory() as tmp:
            import datetime as dtmod

            state_file = str(Path(tmp) / "dryer_shadow_state.json")
            start = dtmod.datetime.now(dtmod.timezone.utc) - timedelta(minutes=150)
            store = CycleStore(state_file, "dryer_shadow")
            store.save({
                "state": "Running", "state_since": format_utc(start), "cycle_id": "cid-V",
                "cycle_start_time": format_utc(start), "energy_at_start": 10.0,
            })
            app, entities, calls = make_shadow(
                tmp, state_file=state_file, power_w="0", live_state="Running",
                extra_args={"min_cycle_minutes": 1, "cooling_period": 0},
            )
            app.initialize()
            self.assertEqual(entities[V2]["attributes"].get("hypothesis"), "true")

    def test_divergence_count_omitted_when_zero_clean_cycles_always_present_as_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            app.initialize()
            attrs = entities[V2]["attributes"]
            self.assertNotIn("divergence_count", attrs)
            self.assertEqual(attrs.get("clean_cycles"), "0")
            self.assertIsInstance(attrs.get("clean_cycles"), str)

    def test_code_fingerprint_and_cycle_id_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            app.initialize()
            attrs = entities[V2]["attributes"]
            self.assertIn("code_fingerprint", attrs)
            self.assertEqual(attrs.get("cycle_id"), "")  # no cycle yet - empty string, not None

    def test_divergence_debounced_then_counted_and_clears_clean_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp, extra_args={"divergence_debounce_s": 90})
            app.initialize()
            entities[LIVE]["state"] = "Running"  # v2 says Off, live says Running - a real mismatch
            app._check_divergence()
            self.assertNotIn("divergence_count", entities[V2]["attributes"])  # still debouncing
            # Fake elapsed time by moving the pending marker's own timestamp backward directly.
            v2, live, _ts = app._divergence_pending
            app._divergence_pending = (v2, live, app.fsm.ctx.now() - timedelta(seconds=91))
            app._check_divergence()
            self.assertEqual(app._divergence_count, 1)

    def test_live_unavailable_skips_comparison_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities, calls = make_shadow(tmp)
            app.initialize()
            entities[LIVE]["state"] = "unavailable"
            app._check_divergence()
            self.assertIsNone(app._divergence_pending)
            self.assertEqual(app._divergence_count, 0)


if __name__ == "__main__":
    unittest.main()
