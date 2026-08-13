# tests/test_washer_restart_survival.py - Wiring cycle_store.py into washer_monitor.py.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# 2026-08-12: HA restarted five times in one day; sensor.washer_state is an AppDaemon
# set_state entity, so each restart erased it and took the cycle clock (cycle_start_time,
# programme, energy, ...) with it. A wash mid-cycle survived only via the power-is-truth
# bootstrap ("sensor is Off but power is 82W - forcing Running"), with a wrong
# cycle_start_time, a lost programme classification, and finish detection blocked behind a
# duration guard it could never reach. cycle_store.py (durable, atomic, tested separately in
# test_appliance_cycle_store.py) is the fix; this suite wires it into initialize().
#
# CRITICAL - do not weaken this suite the way the 2026-07-27 regression shipped:
#   On 2026-07-27, a fix in this exact area shipped strictly worse than no fix at all.
#   initialize() seeded state from a helper, _set_state_entity() recreated the erased sensor
#   with NO attributes, and the restore path immediately after read cycle_start_time back FROM
#   THAT SAME JUST-CREATED ENTITY - so start_time stayed None forever and every timer that
#   could end the cycle was gated on it. It shipped untested because the test of the day
#   STUBBED OUT _restore_running_state - exactly the interaction that was broken.
#
#   Rule for every test below: never stub _restore_running_state, _set_state_entity,
#   _resolve_store_candidate, _infer_boot_start_time_from_history, or
#   _finalize_restored_cycle_identity. Only the AppDaemon surface itself (get_state, set_state,
#   get_history, run_in, log, listen_state/listen_event, call_service, get_app) is faked - the
#   same boundary production draws, and the only place a mock can hide the wrong thing without
#   also hiding a real bug. make_full_init_app() runs initialize() for real end to end; the one
#   place this suite reaches past that boundary is test 11, which drives the real
#   _check_energy_finish() on the app initialize() already produced.

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

NOW = datetime(2026, 8, 12, 14, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return cystore.format_utc(dt)


def make_store_payload(
    state="Running",
    start_time=None,
    saved_at=None,
    cycle_id="cycle-abc",
    notification_sent=False,
    last_door_closed_at=None,
    last_door_closed_trusted=False,
    detected_programme="eco",
    detected_temperature=None,
    energy_at_start=0.10,
    session_cost_kr=0.0,
    started_by="mikkel",
    started_by_method="sole_occupant",
    **extra,
):
    """Build a payload with the exact field names _cycle_store_payload() writes. Uses
    CycleStore.save() for real (see make_full_init_app) so schema/appliance/saved_at are
    stamped exactly like production, not hand-rolled."""
    payload = {
        "state": state,
        "start_time": _iso(start_time) if start_time else "",
        "cycle_id": cycle_id,
        "notification_sent": notification_sent,
        "last_door_closed_at": _iso(last_door_closed_at) if last_door_closed_at else "",
        "last_door_closed_trusted": last_door_closed_trusted,
        "detected_programme": detected_programme,
        "detected_temperature": detected_temperature,
        "energy_at_start": energy_at_start,
        "session_cost_kr": session_cost_kr,
        "started_by": started_by,
        "started_by_method": started_by_method,
    }
    payload.update(extra)
    if saved_at is not None:
        payload["_saved_at_override"] = _iso(saved_at)
    return payload


def make_full_init_app(
    sensor_state=None,
    sensor_attrs=None,
    sensor_last_changed=None,
    helper_state=None,
    power_watts=0.0,
    store_payload=None,
    raw_store_content=None,
    state_history=None,
    power_history=None,
    energy_history=None,
    now=NOW,
    extra_args=None,
):
    """WasherMonitor with initialize() run for real, end to end - including the real
    _restore_running_state, _resolve_store_candidate, _infer_boot_start_time_from_history and
    _finalize_restored_cycle_identity. Only the AppDaemon surface is faked (get_state,
    set_state, get_history, run_in, log, listen_state/listen_event, call_service, get_app).
    feedback_file points at a nonexistent path so initialize() falls back to built-in defaults
    instead of touching real repo data; programmes_file is left unset so the real
    washer_programmes.yaml next to the app loads, matching production classification.
    """
    app = wm.WasherMonitor.__new__(wm.WasherMonitor)
    app.now = now
    app._now_utc = lambda: app.now

    tmpdir = tempfile.mkdtemp(prefix="washer_restart_survival_")
    state_file = os.path.join(tmpdir, "washer_cycle_state.json")
    if store_payload is not None:
        saved_at_override = store_payload.pop("_saved_at_override", None)
        cystore.CycleStore(state_file, "washer").save(store_payload)
        if saved_at_override is not None:
            # CycleStore.save() always stamps saved_at to "now" - only way to test the
            # downtime staleness path is to overwrite it after a real save.
            import json
            with open(state_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            raw["saved_at"] = saved_at_override
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(raw, f)
    elif raw_store_content is not None:
        with open(state_file, "w", encoding="utf-8") as f:
            f.write(raw_store_content)

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
    if extra_args:
        args.update(extra_args)
    app.args = args
    app.AD = None  # getattr(self.AD, "time_zone", None) in initialize()'s timezone resolution

    app.states = {}
    app.attrs_store = {}
    app.last_changed_store = {}
    if sensor_state is not None:
        app.states[args["state_entity"]] = sensor_state
        app.attrs_store[args["state_entity"]] = dict(sensor_attrs or {})
        app.last_changed_store[args["state_entity"]] = _iso(sensor_last_changed or now)
    if helper_state is not None:
        app.states[args["ui_state_entity"]] = helper_state
    app.states[args["power_sensor"]] = power_watts

    app.call_log = []  # shared get_state / _set_state_entity ordering log - see test 9

    def get_state(entity, attribute=None, **kw):
        app.call_log.append(("get_state", entity, attribute))
        if attribute is None:
            return app.states.get(entity)
        if attribute == "all":
            if entity not in app.states and entity not in app.attrs_store:
                return None
            return {
                "state": app.states.get(entity),
                "attributes": dict(app.attrs_store.get(entity, {})),
                "last_changed": app.last_changed_store.get(entity),
                "last_updated": app.last_changed_store.get(entity),
            }
        return (app.attrs_store.get(entity, {}) or {}).get(attribute)

    app.get_state = get_state

    app.set_state_calls = []

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        app.set_state_calls.append(
            {"entity": entity, "state": state, "attributes": attributes, "replace": replace}
        )
        if state is not None:
            app.states[entity] = state
        if attributes is not None:
            if replace:
                app.attrs_store[entity] = dict(attributes)
            else:
                app.attrs_store.setdefault(entity, {}).update(attributes)
        app.last_changed_store[entity] = _iso(app._now_utc())

    app.set_state = set_state

    # The real _set_state_entity, not a stub - wrapped only to record call order for test 9.
    real_set_state_entity = wm.WasherMonitor._set_state_entity

    def set_state_entity(**kwargs):
        app.call_log.append(("_set_state_entity", kwargs.get("state")))
        return real_set_state_entity(app, **kwargs)

    app._set_state_entity = set_state_entity

    app._history = {}
    if state_history:
        app._history[args["state_entity"]] = state_history
    if power_history:
        app._history[args["power_sensor"]] = power_history
    if energy_history:
        app._history[args["energy_sensor"]] = energy_history

    def get_history(entity_id=None, start_time=None, end_time=None, **kw):
        return [list(app._history.get(entity_id, []))]

    app.get_history = get_history

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))

    app.scheduled = []

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw))
        return handle

    app.run_in = run_in
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.call_service = lambda *a, **kw: None
    app.get_app = lambda name: None

    app.initialize()
    return app


def log_messages(app, level=None):
    if level is None:
        return [str(a[0]) for a, kw in app.log_calls]
    return [str(a[0]) for a, kw in app.log_calls if kw.get("level") == level]


def last_publish(app):
    calls = [c for c in app.set_state_calls if c["entity"] == "sensor.washer_state"]
    return calls[-1] if calls else None


class TheIncidentEndToEnd(unittest.TestCase):
    """Case 1: the exact 2026-08-12 shape - store has a Running cycle, the sensor is gone
    (HA restart), the mirror says "Off" (stale/irrelevant), power confirms a real wash."""

    def test_restores_running_with_the_stored_clock(self):
        stored_start = NOW - timedelta(minutes=103)
        payload = make_store_payload(
            state="Running",
            start_time=stored_start,
            cycle_id="cycle-incident",
            detected_programme="eco",
            energy_at_start=0.12,
        )
        app = make_full_init_app(
            sensor_state=None,
            helper_state="Off",
            power_watts=82,
            store_payload=payload,
        )
        self.assertEqual(app.state, "Running")
        self.assertIsNotNone(app.start_time)
        self.assertLessEqual(abs((app.start_time - stored_start).total_seconds()), 2)
        self.assertEqual(app._start_time_source, "durable_store")
        self.assertIsNotNone(app.poll_timer)
        self.assertIsNotNone(app.running_watchdog_timer)
        pub = last_publish(app)
        self.assertIsNotNone(pub)
        self.assertEqual(pub["state"], "Running")
        published_start = cystore.parse_utc(pub["attributes"]["cycle_start_time"])
        self.assertLessEqual(abs((published_start - stored_start).total_seconds()), 2)


class EntityPresentIgnoresStore(unittest.TestCase):
    """Case 2: the AD-only-reload path - a still-live, valid entity must never be overridden
    by (or even meaningfully consult) the durable store, and must not spuriously republish."""

    def test_live_entity_wins_over_a_disagreeing_store(self):
        entity_start = NOW - timedelta(minutes=40)
        sensor_attrs = {
            "cycle_start_time": _iso(entity_start),
            "detected_programme": "strygelet",
            "energy_at_start": "0.05",
        }
        store_payload = make_store_payload(
            state="Running",
            start_time=NOW - timedelta(minutes=200),
            cycle_id="cycle-stale-store",
            detected_programme="bomuld",
        )
        app = make_full_init_app(
            sensor_state="Running",
            sensor_attrs=sensor_attrs,
            sensor_last_changed=entity_start,
            power_watts=40,
            store_payload=store_payload,
        )
        self.assertEqual(app.state, "Running")
        self.assertLessEqual(abs((app.start_time - entity_start).total_seconds()), 2)
        self.assertEqual(app.detected_programme, "strygelet")
        # No republish: the live entity already agreed with itself, so seeded_from_helper was
        # never true and self.state == existing throughout.
        self.assertEqual(app.set_state_calls, [])


class StoreWinsOverDisagreeingHelper(unittest.TestCase):
    """Case 3: entity erased, store says Running, mirror says a different valid state - store
    wins (write order is store -> entity -> helper) and exactly one WARNING names both."""

    def test_store_wins_and_warns_once(self):
        stored_start = NOW - timedelta(minutes=50)
        payload = make_store_payload(state="Running", start_time=stored_start, cycle_id="cycle-3")
        app = make_full_init_app(
            sensor_state=None,
            helper_state="Unemptied",
            power_watts=45,
            store_payload=payload,
        )
        self.assertEqual(app.state, "Running")
        warnings = log_messages(app, "WARNING")
        conflict_warnings = [m for m in warnings if "store" in m.lower() and "Unemptied" in m and "Running" in m]
        self.assertEqual(len(conflict_warnings), 1, warnings)


class NoStoreHelperSeedsUnemptied(unittest.TestCase):
    """Case 4: no store at all, mirror says Unemptied - unchanged behaviour, still seeded."""

    def test_unemptied_seed_still_works(self):
        app = make_full_init_app(sensor_state=None, helper_state="Unemptied", power_watts=0)
        self.assertEqual(app.state, "Unemptied")
        self.assertIsNotNone(app.unemptied_watchdog_timer)
        self.assertIsNotNone(app.unemptied_door_recheck_timer)


class StoreRestoresUnemptiedAndEmptiedInsteadOfDestroyingThem(unittest.TestCase):
    """FIX 2 (2026-08-12 adversarial review): unlike Running/Paused, Unemptied/Emptied carry no
    clock - _resolve_store_candidate deliberately only ever validates Running/Paused - but a
    store record for either must still resolve (and reach the SAME watchdog re-arm an AD-only
    reload or a helper-seed already gets), instead of silently falling through to Off and having
    THAT Off's own publish clear the very record it had just failed to use. Reproduces the exact
    trigger: input_select.washer_state reads unknown/Off/missing during HA's startup window,
    exactly when AppDaemon re-initialises - the entire motivation for the durable store was a
    lost "empty me" reminder, and Unemptied was the one state it didn't cover."""

    def test_unemptied_restores_from_store_and_rearms_its_watchdog(self):
        payload = make_store_payload(state="Unemptied", start_time=None, cycle_id="cycle-unemptied")
        app = make_full_init_app(sensor_state=None, helper_state="Off", power_watts=0, store_payload=payload)
        self.assertEqual(app.state, "Unemptied")
        self.assertIsNotNone(app.unemptied_watchdog_timer)
        self.assertIsNotNone(app.unemptied_door_recheck_timer)
        stored = app._cycle_store.load()
        self.assertIsNotNone(stored, "the durable store was cleared for a state the app could legitimately resume")
        self.assertEqual(stored.get("state"), "Unemptied")

    def test_unemptied_restores_with_an_unknown_helper_too(self):
        payload = make_store_payload(state="Unemptied", start_time=None, cycle_id="cycle-unemptied-unknown")
        app = make_full_init_app(sensor_state=None, helper_state="unknown", power_watts=0, store_payload=payload)
        self.assertEqual(app.state, "Unemptied")
        stored = app._cycle_store.load()
        self.assertIsNotNone(stored)

    def test_emptied_restores_from_store(self):
        # power_watts > 0 so the existing "Emptied + 0W -> self-heal to Off" branch (unrelated
        # to this fix, and correct on its own terms) does not immediately override the
        # store-sourced resolution this test is targeting.
        payload = make_store_payload(state="Emptied", start_time=None, cycle_id="cycle-emptied")
        app = make_full_init_app(sensor_state=None, helper_state=None, power_watts=5, store_payload=payload)
        self.assertEqual(app.state, "Emptied")
        stored = app._cycle_store.load()
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("state"), "Emptied")

    def test_store_wins_over_a_disagreeing_helper_for_unemptied(self):
        payload = make_store_payload(state="Unemptied", start_time=None, cycle_id="cycle-unemptied-2")
        app = make_full_init_app(sensor_state=None, helper_state="Emptied", power_watts=0, store_payload=payload)
        self.assertEqual(app.state, "Unemptied")
        warnings = log_messages(app, "WARNING")
        conflict_warnings = [m for m in warnings if "store" in m.lower() and "Unemptied" in m and "Emptied" in m]
        self.assertEqual(len(conflict_warnings), 1, warnings)


class GateFallsThroughToOff(unittest.TestCase):
    """Case 5: no store, mirror says Running (now seedable), but nothing corroborates a clock
    - 0W and no history. The gate must force Off rather than leave start_time forever None."""

    def test_no_clock_anywhere_falls_through_to_off(self):
        app = make_full_init_app(sensor_state=None, helper_state="Running", power_watts=0)
        self.assertEqual(app.state, "Off")
        self.assertIsNone(app.start_time)
        warnings = log_messages(app, "WARNING")
        self.assertTrue(any("falling through to Off" in m for m in warnings), warnings)


class WideningIsSafeWithHistory(unittest.TestCase):
    """Case 6: no store, mirror says Running, power corroborates, and state-entity history
    shows a real Off -> Running transition ~100 min ago - the widened seed resolves to Running
    with that inferred clock instead of being rejected."""

    def test_helper_seed_plus_history_yields_running(self):
        transition_at = NOW - timedelta(minutes=100)
        history = [
            {"state": "Off", "last_changed": _iso(NOW - timedelta(hours=5))},
            {"state": "Running", "last_changed": _iso(transition_at)},
        ]
        app = make_full_init_app(
            sensor_state=None,
            helper_state="Running",
            power_watts=82,
            state_history=history,
        )
        self.assertEqual(app.state, "Running")
        self.assertIsNotNone(app.start_time)
        self.assertLessEqual(abs((app.start_time - transition_at).total_seconds()), 2)
        self.assertEqual(app._start_time_source, "state_history")
        self.assertIsNotNone(app.poll_timer)
        self.assertIsNotNone(app.running_watchdog_timer)


class StaleStoreIsRejected(unittest.TestCase):
    """Case 7: store has a Running record whose start_time is 14h old (> max_running_hours,
    default 5h) - reject it and warn, exactly like a genuinely-stuck cycle would be rejected."""

    def test_old_start_time_is_rejected(self):
        payload = make_store_payload(
            state="Running", start_time=NOW - timedelta(hours=14), cycle_id="cycle-stale",
        )
        app = make_full_init_app(sensor_state=None, helper_state=None, power_watts=0, store_payload=payload)
        self.assertEqual(app.state, "Off")
        warnings = log_messages(app, "WARNING")
        self.assertTrue(any("h old" in m and "ignoring" in m for m in warnings), warnings)

    def test_long_ago_save_is_rejected_even_with_a_recent_start_time(self):
        """store_max_downtime_hours (default 12h): saved_at itself is old (AppDaemon or the
        whole box was down a long time), independent of the max_running_hours check above -
        both are stale rejections, but on different clocks."""
        payload = make_store_payload(
            state="Running", start_time=NOW - timedelta(hours=1), cycle_id="cycle-old-save",
            saved_at=NOW - timedelta(hours=20),
        )
        app = make_full_init_app(sensor_state=None, helper_state=None, power_watts=0, store_payload=payload)
        self.assertEqual(app.state, "Off")
        warnings = log_messages(app, "WARNING")
        self.assertTrue(any("saved" in m and "ago" in m and "ignoring" in m for m in warnings), warnings)

    def test_future_start_time_is_rejected(self):
        """FIX 3 (2026-08-12 adversarial review): _resolve_store_candidate used to test only
        age_hours > max_running_hours - a NEGATIVE age (start_time in the future, e.g. the box's
        clock was behind real time when it was saved, before NTP synced) passed straight
        through. A store clock hours in the future would then restore as Running with the poll
        timer and watchdog armed, and every duration guard would compare against a negative run
        length and could never pass - a permanently wedged Running. Reject it exactly like a
        genuinely-stuck (too old) cycle is rejected, rather than trusting an impossible clock."""
        payload = make_store_payload(
            state="Running", start_time=NOW + timedelta(hours=3), cycle_id="cycle-future",
        )
        app = make_full_init_app(sensor_state=None, helper_state=None, power_watts=0, store_payload=payload)
        self.assertEqual(app.state, "Off")
        self.assertIsNone(app.start_time)
        warnings = log_messages(app, "WARNING")
        self.assertTrue(any("future" in m.lower() and "ignoring" in m for m in warnings), warnings)


class ContaminationIsCorrected(unittest.TestCase):
    """Case 8: store's start_time is stale from a PREVIOUS cycle - power history shows a
    90-minute idle gap after it, then sustained power resuming later. The existing
    power-history cross-check inside _restore_running_state (unconditional, no rank gate)
    must still fire for a store-sourced start_time, correcting it past the gap."""

    def test_start_time_corrected_past_the_idle_gap(self):
        stored_start = NOW - timedelta(minutes=180)
        gap_start = stored_start + timedelta(minutes=5)
        resumed = gap_start + timedelta(minutes=90)
        power_history = [
            {"state": "85", "last_changed": _iso(stored_start - timedelta(minutes=2))},
            {"state": "2", "last_changed": _iso(gap_start)},
            {"state": "1.5", "last_changed": _iso(gap_start + timedelta(minutes=45))},
            {"state": "90", "last_changed": _iso(resumed)},
            {"state": "92", "last_changed": _iso(resumed + timedelta(minutes=2))},
        ]
        payload = make_store_payload(
            state="Running", start_time=stored_start, cycle_id="cycle-contaminated",
            notification_sent=True,
        )
        app = make_full_init_app(
            sensor_state=None,
            helper_state=None,
            power_watts=90,
            store_payload=payload,
            power_history=power_history,
        )
        self.assertEqual(app.state, "Running")
        self.assertIsNotNone(app.start_time)
        self.assertGreater(app.start_time, stored_start)
        self.assertLessEqual(abs((app.start_time - resumed).total_seconds()), 120)
        self.assertEqual(app._start_time_source, "power_history")
        # Contamination detected -> _finalize_restored_cycle_identity must not carry the
        # stale notification_sent (or cycle_id) over to what is, in every way that matters, a
        # different cycle than the one the store thought it was tracking.
        self.assertFalse(app.notification_sent)
        self.assertNotEqual(app._cycle_id, "cycle-contaminated")


class OrderingNeverWritesBeforeTheBootRead(unittest.TestCase):
    """Case 9: the test that would have caught 2026-07-27 - no _set_state_entity call may
    precede the boot-resolution snapshot read (get_state(state_entity, attribute="all")), and -
    the actual invariant the incident broke - no restore-relevant get_state(state_entity, ...)
    call may follow the FIRST _set_state_entity write either.

    A bare first(all-read) < first(write) check alone is too weak: it also PASSES against the
    pre-fix code, which did one same-statement "all" read immediately before its first write
    (inside the old recovery_off_to_running branch), then went on to re-read the live entity 15
    more times from inside the old, parameterless _restore_running_state - including a direct
    attribute="cycle_start_time" read - exactly what let a just-recreated, attribute-less entity
    get read straight back for its own cycle_start_time, wedging it at None forever."""

    # Reads of state_entity that legitimately happen after the boot-restore dispatch has
    # already finished, for reasons unrelated to restoring anything, so excluded below:
    # "last_off_at" is read by _cycle_store_payload purely to persist it forward into the next
    # store write (a write-side concern, not a restore one), and a bare (attribute=None) read is
    # the ordinary post-boot bootstrap tick (initialize()'s trailing _power_changed call)
    # checking the state initialize() itself already just published.
    _BENIGN_POST_WRITE_ATTRS = ("last_off_at", None)

    def test_boot_snapshot_precedes_every_write(self):
        stored_start = NOW - timedelta(minutes=30)
        payload = make_store_payload(state="Running", start_time=stored_start, cycle_id="cycle-9")
        app = make_full_init_app(
            sensor_state=None, helper_state="Off", power_watts=82, store_payload=payload,
        )
        all_read_indices = [
            i for i, c in enumerate(app.call_log)
            if c[0] == "get_state" and c[2] == "all"
        ]
        write_indices = [i for i, c in enumerate(app.call_log) if c[0] == "_set_state_entity"]
        self.assertTrue(all_read_indices, "expected a get_state(attribute='all') boot snapshot read")
        self.assertTrue(write_indices, "expected at least one _set_state_entity write")
        self.assertLess(
            all_read_indices[0], write_indices[0],
            f"a write happened before the boot snapshot read: {app.call_log}",
        )

        after_first_write = app.call_log[write_indices[0] + 1:]
        suspect_reads = [
            c for c in after_first_write
            if c[0] == "get_state" and c[1] == app.state_entity
            and c[2] not in self._BENIGN_POST_WRITE_ATTRS
        ]
        self.assertEqual(
            suspect_reads, [],
            f"a restore-relevant get_state({app.state_entity!r}, ...) call happened after the "
            f"first write - restore must consume the boot snapshot/store captured up front, "
            f"never a fresh read of an entity the write may have just (re)created with no "
            f"attributes: {suspect_reads}",
        )
        self.assertIsNotNone(app.start_time)


class EveryRunningPausedBootPathHasAClock(unittest.TestCase):
    """Case 10: parameterised over every path that can end in Running/Paused - start_time is
    never None and both the poll timer and the running watchdog are always armed."""

    def _assert_invariant(self, app):
        self.assertIn(app.state, ("Running", "Paused"))
        self.assertIsNotNone(app.start_time)
        self.assertIsNotNone(app.poll_timer)
        self.assertIsNotNone(app.running_watchdog_timer)

    def test_store_running(self):
        payload = make_store_payload(state="Running", start_time=NOW - timedelta(minutes=20), cycle_id="a")
        app = make_full_init_app(sensor_state=None, power_watts=50, store_payload=payload)
        self._assert_invariant(app)

    def test_store_paused(self):
        payload = make_store_payload(state="Paused", start_time=NOW - timedelta(minutes=20), cycle_id="b")
        app = make_full_init_app(sensor_state=None, power_watts=0, store_payload=payload)
        self._assert_invariant(app)

    def test_helper_seed_plus_history(self):
        history = [
            {"state": "Off", "last_changed": _iso(NOW - timedelta(hours=4))},
            {"state": "Running", "last_changed": _iso(NOW - timedelta(minutes=60))},
        ]
        app = make_full_init_app(
            sensor_state=None, helper_state="Running", power_watts=82, state_history=history,
        )
        self._assert_invariant(app)

    def test_legacy_power_is_truth_recovery_with_store(self):
        """Entity literally says "Off" (not erased) but power is high - the legacy recovery
        path, now consulting the store for its clock instead of a fresh inference."""
        payload = make_store_payload(state="Running", start_time=NOW - timedelta(minutes=15), cycle_id="c")
        app = make_full_init_app(sensor_state="Off", power_watts=82, store_payload=payload)
        self._assert_invariant(app)

    def test_ad_reload_running(self):
        entity_start = NOW - timedelta(minutes=10)
        app = make_full_init_app(
            sensor_state="Running",
            sensor_attrs={"cycle_start_time": _iso(entity_start)},
            sensor_last_changed=entity_start,
            power_watts=50,
        )
        self._assert_invariant(app)

    def test_ad_reload_paused(self):
        """Previously the silent gap: a restored Paused got no restore call at all (no timer
        re-arm), because initialize() only ever checked self.state == "Running"."""
        entity_start = NOW - timedelta(minutes=10)
        app = make_full_init_app(
            sensor_state="Paused",
            sensor_attrs={"cycle_start_time": _iso(entity_start)},
            sensor_last_changed=entity_start,
            power_watts=0,
        )
        self._assert_invariant(app)


class TrustedDoorCloseDisarmsCycleTwoHeuristics(unittest.TestCase):
    """Case 11: a store-restored last_door_closed_trusted=True must gate off BOTH "cycle 2"
    heuristics inside _check_energy_finish on a subsequent real tick - the highest-value field
    in the store, per the task brief, precisely because it disarms both at once."""

    def test_neither_heuristic_fires_after_restore(self):
        stored_start = NOW - timedelta(minutes=60)
        door_close = stored_start - timedelta(seconds=20)  # trusted, and just before start
        payload = make_store_payload(
            state="Running",
            start_time=stored_start,
            cycle_id="cycle-11",
            last_door_closed_at=door_close,
            last_door_closed_trusted=True,
            detected_programme="eco",
        )
        app = make_full_init_app(sensor_state=None, power_watts=50, store_payload=payload)
        self.assertTrue(app.last_door_closed_trusted)
        self.assertIsNotNone(app.last_door_closed_at)

        before = app.start_time
        app._check_energy_finish({})
        self.assertEqual(app.start_time, before)
        info_and_debug = log_messages(app, "INFO") + log_messages(app, "DEBUG")
        self.assertFalse(any("cycle 2" in m.lower() or "cycle-2" in m.lower() for m in info_and_debug))


class NotificationSentIsScopedByCycleId(unittest.TestCase):
    """Case 12: a stored notification_sent=True is trusted only for the SAME cycle identity
    (matching cycle_id, i.e. the stored clock survived the contamination cross-check intact);
    a different (regenerated) cycle_id after contamination must never suppress a real
    announcement for what is treated as a fresh cycle."""

    def test_matching_identity_keeps_notification_sent(self):
        stored_start = NOW - timedelta(minutes=45)
        payload = make_store_payload(
            state="Running", start_time=stored_start, cycle_id="cycle-match",
            notification_sent=True,
        )
        app = make_full_init_app(sensor_state=None, power_watts=50, store_payload=payload)
        self.assertEqual(app._start_time_source, "durable_store")
        self.assertEqual(app._cycle_id, "cycle-match")
        self.assertTrue(app.notification_sent)

    def test_contaminated_identity_does_not_suppress_announcement(self):
        stored_start = NOW - timedelta(minutes=180)
        gap_start = stored_start + timedelta(minutes=5)
        resumed = gap_start + timedelta(minutes=90)
        power_history = [
            {"state": "85", "last_changed": _iso(stored_start - timedelta(minutes=2))},
            {"state": "2", "last_changed": _iso(gap_start)},
            {"state": "1.5", "last_changed": _iso(gap_start + timedelta(minutes=45))},
            {"state": "90", "last_changed": _iso(resumed)},
            {"state": "92", "last_changed": _iso(resumed + timedelta(minutes=2))},
        ]
        payload = make_store_payload(
            state="Running", start_time=stored_start, cycle_id="cycle-mismatch",
            notification_sent=True,
        )
        app = make_full_init_app(
            sensor_state=None, power_watts=90, store_payload=payload, power_history=power_history,
        )
        self.assertNotEqual(app._cycle_id, "cycle-mismatch")
        self.assertFalse(app.notification_sent)


class CorruptStoreDegradesGracefully(unittest.TestCase):
    """Case 13: a corrupt store file must never escape initialize() - one WARNING, and the app
    falls back to whatever today's logic (helper/power) would otherwise produce."""

    def test_corrupt_json_falls_back_to_plain_restore(self):
        app = make_full_init_app(
            sensor_state=None, helper_state="Unemptied", power_watts=0,
            raw_store_content="{not valid json!!",
        )
        self.assertEqual(app.state, "Unemptied")
        warnings = log_messages(app, "WARNING")
        store_warnings = [m for m in warnings if "cyclestore" in m.lower() or "store" in m.lower()]
        self.assertTrue(store_warnings, warnings)

    def test_corrupt_json_with_no_other_signal_lands_on_off(self):
        app = make_full_init_app(
            sensor_state=None, helper_state=None, power_watts=0,
            raw_store_content="{not valid json!!",
        )
        self.assertEqual(app.state, "Off")


class PublishedStateNotSelfStateDrivesTheStore(unittest.TestCase):
    """FIX 1 (2026-08-12 adversarial review): the durable-store payload must record the state
    actually being PUBLISHED (the state= kwarg _set_state_entity received), never self.state -
    and the store may only be cleared when self.state itself (not just the published string) is
    genuinely Off. Reachable in production via _on_confirm_changed (washer_monitor.py), which
    republishes state=self.get_state(state_entity) - a value read back from HA, not self.state -
    so the documented AD 4.5.13 stale-get_state behaviour, or the entity being erased at the
    exact moment the user touches the programme selector, can hand that call a state string that
    disagrees with self.state. Both tests call the real _set_state_entity a second time on an
    already-initialized app - the same "reach past the boundary on a real method" pattern
    NotificationSentIsScopedByCycleId's contamination test and test 11 already use - never
    stubbed, per this suite's CRITICAL rule."""

    def test_payload_records_the_published_state_not_self_state(self):
        payload = make_store_payload(state="Running", start_time=NOW - timedelta(minutes=10), cycle_id="cycle-mismatch")
        app = make_full_init_app(sensor_state=None, power_watts=50, store_payload=payload)
        self.assertEqual(app.state, "Running")

        # Simulate _on_confirm_changed's exact shape: publish a state string that disagrees
        # with self.state (self.state is deliberately left at "Running" here).
        app._set_state_entity(state="Unemptied", attributes={}, replace=False)

        stored = app._cycle_store.load()
        self.assertIsNotNone(stored)
        self.assertEqual(stored.get("state"), "Unemptied")

    def test_off_publish_does_not_clear_the_store_when_self_state_is_still_live(self):
        payload = make_store_payload(state="Running", start_time=NOW - timedelta(minutes=10), cycle_id="cycle-live")
        app = make_full_init_app(sensor_state=None, power_watts=50, store_payload=payload)
        self.assertEqual(app.state, "Running")

        # A publish claiming Off while self.state is still Running - current_state =
        # self.get_state(self.state_entity) or "Running" in _on_confirm_changed can read a
        # stale "Off" while self.state never changed - must not destroy the live cycle's
        # durable clock.
        app._set_state_entity(state="Off", attributes={}, replace=False)

        stored = app._cycle_store.load()
        self.assertIsNotNone(stored, "a mismatched Off publish destroyed a live cycle's durable store")
        self.assertEqual(stored.get("state"), "Running")


class StateSinceSurvivesConsecutiveRestarts(unittest.TestCase):
    """FIX 4 (2026-08-12 adversarial review): _store_state_since used to be stamped AFTER the
    save it feeds (one write behind) and never restored on boot, so every restart reset it to
    that boot's own "now" instead of carrying the original entry-into-state time forward.
    Reproduces the review's own two-consecutive-restarts shape with a store seeded with an
    explicit state_since (as a real record - already Running before the FIRST of the two
    restarts - would have)."""

    def test_state_since_carries_forward_across_two_restarts(self):
        stored_start = NOW - timedelta(hours=2)
        payload = make_store_payload(
            state="Running", start_time=stored_start, cycle_id="cycle-since",
            state_since=_iso(stored_start),
        )
        app1 = make_full_init_app(sensor_state=None, power_watts=50, store_payload=payload, now=NOW)
        self.assertEqual(app1.state, "Running")
        shared_state_file = app1.args["state_file"]
        stored_after_first = app1._cycle_store.load()
        self.assertIsNotNone(stored_after_first)
        first_state_since = stored_after_first.get("state_since")
        self.assertEqual(first_state_since, _iso(stored_start))

        later = NOW + timedelta(hours=1)
        app2 = make_full_init_app(
            sensor_state=None, power_watts=50, now=later,
            extra_args={"state_file": shared_state_file},
        )
        self.assertEqual(app2.state, "Running")
        stored_after_second = app2._cycle_store.load()
        self.assertIsNotNone(stored_after_second)
        self.assertEqual(
            stored_after_second.get("state_since"), first_state_since,
            "state_since reset to this boot's own time instead of carrying the original entry "
            "time forward across a restart",
        )


if __name__ == "__main__":
    unittest.main()
