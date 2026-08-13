# tests/test_dishwasher_restart_survival.py - Restart-survival coverage for the on-disk
# cycle store (cycle_store.py) wired into dishwasher_monitor.py's initialize().
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# IMPORTANT: this file NEVER stubs _restore_cycle_tracking_from_entity. The 2026-07-27
# incident this whole feature exists to fix shipped strictly worse than no fix BECAUSE its
# test stubbed out the restore path - precisely the interaction that was broken: initialize()
# seeded state from a helper, _set_state_entity(state=...) recreated the erased sensor with no
# attributes, and the restore path immediately after read cycle_start_time back from that same
# just-created entity, so start_time stayed None forever while state stayed Running with no
# armed exit (and dishwasher_monitor.py's own boot self-heal - gated on self.start_time - was
# dead code as a direct result). Every test below runs the REAL initialize() and the REAL
# restore method it dispatches to, against a faithful get_state() double that distinguishes a
# bare get_state(entity) call from get_state(entity, attribute="all") - the earlier harnesses
# in this directory collapse both to the same bare value, which is exactly why they have to
# stub the restore methods to stay usable.

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from datetime import timedelta, timezone
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


def _entity_get_state(entities, call_log):
    """A get_state() double that actually distinguishes attribute="all" from a bare call -
    the thing the existing harnesses in this directory cannot do, which is why they stub the
    restore method instead of letting a real boot-attrs read happen.

    Each call_log entry also records the immediate caller's function name (via
    sys._getframe(1), the frame that invoked self.get_state(...) directly -
    dishwasher_monitor.py's get_state is a plain function assigned straight onto the app
    instance, not a bound method, so there is no intermediate wrapper frame in between) -
    OrderingProvesTheBootSnapshotPrecedesTheFirstWrite below uses this to narrowly identify the
    specific post-write reads that are structurally incapable of restoring cycle_start_time,
    rather than allowing any attribute value or caller wholesale."""

    def get_state(entity, attribute=None, **kwargs):
        caller = sys._getframe(1).f_code.co_name
        rec = entities.get(entity)
        if attribute == "all":
            call_log.append(("get_state_all", entity, caller))
            if rec is None:
                return None
            return {
                "state": rec.get("state"),
                "attributes": dict(rec.get("attributes") or {}),
                "last_changed": rec.get("last_changed"),
                "last_updated": rec.get("last_changed"),
            }
        call_log.append(("get_state", entity, caller))
        if rec is None:
            return None
        if attribute:
            return (rec.get("attributes") or {}).get(attribute)
        return rec.get("state")

    return get_state


def make_app(
    tmpdir,
    *,
    sensor_state=None,
    sensor_attrs=None,
    sensor_last_changed=None,
    helper_state=None,
    power_w="0",
    energy_kwh="10.0",
    door_state="off",
    extra_args=None,
):
    """Real DishwasherMonitor with initialize() run for real - see the module docstring for
    why the restore method is never stubbed here. Every test-only file (feedback, programmes,
    cycle-state store) lives under `tmpdir` so nothing ever touches, or races on, the real
    apps/appliances/dishwasher_*.json files.

    _set_state_entity is ALSO never stubbed (FIX 2, 2026-08-12 review): only self.set_state -
    the actual AppDaemon primitive it calls - is faked below, so _save_cycle_state /
    _build_cycle_store_payload / CycleStore.save all run for real on every boot, writing to
    the tmpdir-scoped state_file below (exposed as app.state_file so a test can read back what
    actually landed on disk). Before this fix, every test in this file stubbed
    _set_state_entity wholesale, so that entire write path was exercised by zero tests - the
    same shape of gap the 2026-07-27 incident this suite is named for shipped through."""
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)

    entities = {
        STATE_ENTITY: {"state": sensor_state, "attributes": dict(sensor_attrs or {}), "last_changed": sensor_last_changed},
        UI_SELECT: {"state": helper_state, "attributes": {}, "last_changed": None},
        POWER_SENSOR: {"state": power_w, "attributes": {}, "last_changed": None},
        ENERGY_SENSOR: {"state": energy_kwh, "attributes": {}, "last_changed": None},
        DOOR_SENSOR: {"state": door_state, "attributes": {}, "last_changed": None},
    }
    app._test_entities = entities

    call_log = []
    app.call_log = call_log
    app.get_state = _entity_get_state(entities, call_log)
    app.get_history = lambda **kwargs: []

    state_file = str(Path(tmpdir) / "dishwasher_cycle_state.json")
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
        "feedback_file": str(Path(tmpdir) / "dishwasher_feedback_test.json"),
        "programmes_file": str(Path(tmpdir) / "dishwasher_programmes_test.yaml"),
        "state_file": state_file,
    }
    if extra_args:
        args.update(extra_args)
    app.args = args
    app.state_file = args["state_file"]  # tests read this back via CycleStore.load()

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_app = lambda name: None
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

    # Only the AppDaemon primitive is faked, not _set_state_entity itself - see the docstring
    # above. entity_id is always self.state_entity in production (the only caller), but
    # setdefault keeps this generic rather than assuming that.
    app.set_state_calls = []

    def set_state(entity_id, **kw):
        call_log.append(("set_state", entity_id, sys._getframe(1).f_code.co_name))
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
    """Write `payload` straight to the on-disk store this app instance will load from -
    mirrors what a real prior boot would have left behind."""
    store = cs.CycleStore(Path(tmpdir) / f"{appliance}_cycle_state.json", appliance)
    assert store.save(payload)
    return store


def scheduled_callbacks(app):
    return [cb for cb, _delay, _kw in app.scheduled]


def warning_logs(app):
    return [a for a, kw in app.log_calls if kw.get("level") == "WARNING"]


def boot_resolution_warnings(app):
    """WARNINGs from the boot state-resolution logic specifically - excludes incidental
    harness noise (missing programmes_file, no SonosNotifier app) that has nothing to do with
    it."""
    return [
        a for a in warning_logs(app)
        if "cyclestore" in str(a[0]).lower() or "boot resolve" in str(a[0]).lower()
    ]


def _is_benign_post_write_state_entity_read(entry):
    """True if `entry` (a call_log tuple: kind, entity, caller) is a get_state(state_entity, ...)
    read that is structurally incapable of feeding a restored cycle_start_time back into
    instance state - see OrderingProvesTheBootSnapshotPrecedesTheFirstWrite's docstring for why
    this check exists. Only two shapes are allowed, both established by reading
    dishwasher_monitor.py, not assumed:

    - kind == "get_state" (attribute=None, a bare read): returns the state STRING only - HA/
      AppDaemon never hands back attributes without an explicit attribute= - so this can never
      carry cycle_start_time (which lives exclusively in attributes) regardless of caller. Used
      for plain "what is the published state right now" control-flow checks (_power_changed's
      current_state, the guard at the top of _update_running_attributes, _should_change_state,
      the *_watchdog_timeout / _poll_power / _tick_classify methods) - never by a restore
      method, which always consumes the cached
      _boot_full_state_snapshot()/_boot_store_snapshot() instead of a live call.

    - kind == "get_state_all" (attribute="all") AND caller == "_update_running_attributes": the
      one reachable post-write "all" read, and the dangerous shape in general (this is exactly
      how the original 2026-07-27 bug read cycle_start_time back) - narrowed to this ONE caller
      specifically. Its result seeds a merge dict that is immediately overwritten
      (existing.update(attrs)) by fresh values computed from self.start_time - already restored,
      moments earlier, from the cached snapshot - including cycle_start_time itself, before
      anything is republished (state="Running" is passed as a literal there too, not read back).
      Never assigned to self.start_time or any other restore decision. Any OTHER caller doing an
      "all" read here remains suspect - this does not blanket-allow the attribute value (the
      dishwasher has several more "all" readers - force_off/force_unemptied/force_emptied
      handlers, pause-finish checks - but none of them run synchronously during initialize()).
    """
    kind, _entity, caller = entry
    if kind == "get_state":
        return True
    return kind == "get_state_all" and caller == "_update_running_attributes"


class TheIncidentEndToEnd(unittest.TestCase):
    """The 2026-07-27 incident, reproduced and fixed: a Running wash in progress when HA
    restarts must come back Running, with its real start_time and both exit timers armed -
    not silently revert to Off, and not sit in Running forever with nothing that can ever end
    it."""

    def test_running_cycle_survives_ha_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(
                tmp,
                sensor_state=None,  # HA erased the entity
                helper_state="Off",  # stale mirror value, must not win over the store
                power_w="900",  # comfortably above start_w
            )
            now = app._now_utc()
            stored_start = now - timedelta(minutes=103)
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(stored_start),
                "energy_at_start": "0.20",
                "detected_programme": "eco",
                "expected_dur_at_start": 227,
                "max_power_w": 1800.0,
                "notification_sent": False,
            })

            app.initialize()

            self.assertEqual(app.state, "Running")
            self.assertIsNotNone(app.start_time)
            self.assertLessEqual(abs((app.start_time - stored_start).total_seconds()), 2)
            self.assertIn(app._poll_power, scheduled_callbacks(app))
            self.assertIn(app._running_watchdog_timeout, scheduled_callbacks(app))
            published = entities[STATE_ENTITY]["attributes"]
            self.assertEqual(published.get("cycle_start_time"), cs.format_utc(stored_start))


class EntityValidStoreIgnored(unittest.TestCase):
    """AD-only-reload path (entity itself survived): the entity always wins outright, exactly
    as before this change - the store is not even consulted for the state NAME, even when it
    disagrees."""

    def test_present_valid_entity_wins_over_conflicting_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(
                tmp,
                sensor_state="Paused",
                sensor_attrs={
                    "cycle_start_time": "2026-08-12T09:00:00Z",
                    "energy_at_start": "1.0",
                    "detected_programme": "eco",
                    "programme_duration_min": 227,
                },
                sensor_last_changed="2026-08-12T09:50:00Z",
                power_w="900",
            )
            seed_store(tmp, "dishwasher", {
                "state": "Unemptied",  # deliberately conflicting with the entity
                "cycle_start_time": "2020-01-01T00:00:00Z",
            })

            app.initialize()

            self.assertEqual(app.state, "Paused")
            self.assertEqual(cs.format_utc(app.start_time), "2026-08-12T09:00:00Z")
            self.assertEqual(
                [a for a, kw in app.log_calls if "cyclestore says" in str(a[0]).lower()],
                [],
            )


class StoreBeatsHelperWithOneWarning(unittest.TestCase):
    """Write order is store -> entity -> helper, so the store can never be behind the mirror -
    it wins on a disagreement, but that disagreement is still worth exactly one WARNING."""

    def test_store_running_wins_over_helper_unemptied(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state="Unemptied", power_w="900")
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=20)),
                "expected_dur_at_start": 227,
            })

            app.initialize()

            self.assertEqual(app.state, "Running")
            warnings = boot_resolution_warnings(app)
            self.assertEqual(len(warnings), 1)
            msg = str(warnings[0][0])
            self.assertIn("Running", msg)
            self.assertIn("Unemptied", msg)


class NoStoreHelperUnemptiedStillSeeds(unittest.TestCase):
    """Preserves the 2026-07-28 behaviour: a clock-free state seeded from the mirror when
    nothing else has an opinion still reaches its restore branch."""

    def test_no_store_helper_unemptied_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state="Unemptied", power_w="0")

            app.initialize()

            self.assertEqual(app.state, "Unemptied")


class NoStoreHelperRunningZeroWattsFallsThroughToOff(unittest.TestCase):
    """The gate: a helper claiming Running never carries a clock on its own, and here nothing
    else (entity, store) supplies one either - so it must fall through to Off, not Running with
    no armed exit."""

    def test_helper_running_alone_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state="Running", power_w="0")

            app.initialize()

            self.assertEqual(app.state, "Off")
            self.assertIsNone(app.start_time)


class StaleStoreStartTimeIsRejected(unittest.TestCase):
    """A stored Running whose clock is far older than max_running_hours is more likely an
    abandoned/wedged write than a cycle still worth resuming - reject it, with a WARNING,
    rather than trust it blindly."""

    def test_14h_old_start_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(hours=14)),
                "expected_dur_at_start": 227,
            })

            app.initialize()

            self.assertEqual(app.state, "Off")
            warnings = boot_resolution_warnings(app)
            self.assertEqual(len(warnings), 1)
            self.assertIn("old", str(warnings[0][0]).lower())


class OrderingProvesTheBootSnapshotPrecedesTheFirstWrite(unittest.TestCase):
    """The test that would have caught 2026-07-27: the boot-time attribute="all" read that
    feeds start_time must happen before the FIRST _set_state_entity write of this boot - not
    after it, which is what let a just-recreated, attribute-less entity get read straight back
    for its own cycle_start_time.

    A bare first(all-read) < first(write) check alone is too weak (2026-08-12 adversarial
    review, second pass): it passes as soon as ANY boot-time "all" read exists anywhere before
    ANY write, whether or not that read is the one start_time actually comes from, and it says
    nothing at all about what happens AFTER the first write - exactly where the 2026-07-27 bug
    lived (a restore method reading the entity back live, post-write, after that same write had
    just recreated it with no attributes). The real invariant is the second one below: no
    restore-relevant get_state(state_entity, ...) call may happen after the FIRST write either -
    _restore_cycle_tracking_from_entity must consume the boot snapshot captured up front. See
    _is_benign_post_write_state_entity_read for the two narrow, specifically-justified
    exceptions actually reachable in this file's boot path - established by reading
    dishwasher_monitor.py, not assumed, and deliberately not widened to make this test pass."""

    def test_boot_attrs_read_precedes_first_entity_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state="Off", power_w="900")
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=30)),
                "expected_dur_at_start": 227,
            })

            app.initialize()

            first_write_idx = next(
                i for i, entry in enumerate(app.call_log) if entry[0] == "set_state"
            )
            first_boot_read_idx = next(
                i for i, entry in enumerate(app.call_log)
                if entry[0] == "get_state_all" and entry[1] == STATE_ENTITY
            )
            self.assertLess(
                first_boot_read_idx, first_write_idx,
                "the attribute=\"all\" boot snapshot must be read before the first "
                "_set_state_entity write, not after it",
            )

            suspect_reads = [
                entry for entry in app.call_log[first_write_idx + 1:]
                if entry[1] == STATE_ENTITY
                and entry[0] in ("get_state", "get_state_all")
                and not _is_benign_post_write_state_entity_read(entry)
            ]
            self.assertEqual(
                suspect_reads, [],
                f"a restore-relevant get_state({STATE_ENTITY!r}, ...) call happened after the "
                f"first write - restore must consume the boot snapshot/store captured up front, "
                f"never a fresh read of an entity the write may have just (re)created with no "
                f"attributes: {suspect_reads}",
            )
            self.assertIsNotNone(app.start_time)


class RestoredRunningOrPausedAlwaysHasAStartTimeAndArmedTimers(unittest.TestCase):
    """Parameterised post-condition: however Running/Paused was reached at boot - entity or
    store sourced - start_time must be set and both the poll timer and the running watchdog
    must be armed. (2026-07-27 produced a Running with no armed exit; this pins the opposite,
    and also proves the boot self-heal below is no longer permanently dead code.)"""

    def test_every_running_or_paused_boot_path_has_start_time_and_armed_timers(self):
        now_ref = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)._now_utc()
        cases = {
            "entity_running": dict(
                sensor_state="Running",
                sensor_attrs={"cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "programme_duration_min": 227},
                helper_state=None,
                store_payload=None,
            ),
            "entity_paused": dict(
                sensor_state="Paused",
                sensor_attrs={"cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "programme_duration_min": 227},
                sensor_last_changed=cs.format_utc(now_ref - timedelta(minutes=2)),
                helper_state=None,
                store_payload=None,
            ),
            "store_running": dict(
                sensor_state=None,
                helper_state=None,
                store_payload={"state": "Running", "cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "expected_dur_at_start": 227},
            ),
            "store_paused": dict(
                sensor_state=None,
                helper_state=None,
                store_payload={"state": "Paused", "cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "expected_dur_at_start": 227},
            ),
        }
        for name, cfg in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    kwargs = {k: v for k, v in cfg.items() if k != "store_payload"}
                    app, entities = make_app(tmp, power_w="900", **kwargs)
                    if cfg.get("store_payload"):
                        seed_store(tmp, "dishwasher", cfg["store_payload"])

                    app.initialize()

                    self.assertIn(app.state, ("Running", "Paused"))
                    self.assertIsNotNone(app.start_time, f"{name}: start_time must not be None")
                    self.assertIn(app._poll_power, scheduled_callbacks(app), f"{name}: poll timer not armed")
                    self.assertIn(
                        app._running_watchdog_timeout, scheduled_callbacks(app),
                        f"{name}: running watchdog not armed",
                    )


class CorruptStoreDegradesGracefully(unittest.TestCase):
    """A corrupt on-disk file must never take the app down with it - one WARNING, then normal
    initialization from whatever other evidence exists."""

    def test_corrupt_store_file_logs_one_warning_and_initializes_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "dishwasher_cycle_state.json"
            state_path.write_text("{not valid json at all")
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")

            try:
                app.initialize()
            except Exception as e:  # pragma: no cover - the whole point is that this can't happen
                self.fail(f"initialize() raised on a corrupt store: {e}")

            self.assertEqual(app.state, "Off")
            store_warnings = [a for a, kw in app.log_calls if kw.get("level") == "WARNING" and "cyclestore" in str(a[0]).lower()]
            self.assertEqual(len(store_warnings), 1)


class StoreTimestampFormatsBothParse(unittest.TestCase):
    """A stored start_time in either wire format (Z suffix or an explicit offset) must resolve
    to the same tz-aware UTC instant - see cycle_store.parse_utc."""

    def test_z_suffix_and_explicit_offset_both_restore_the_same_instant(self):
        # Anchor relative to now, never a fixed wall-clock string. A hardcoded timestamp ages
        # past max_running_hours (5h) within the same day, and the staleness guard then
        # rejects the payload outright - which fails this test looking exactly like a parse
        # bug when nothing is wrong with parsing at all.
        anchor = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)._now_utc().replace(microsecond=0) - timedelta(minutes=45)
        expected = cs.format_utc(anchor)
        for label, ts in (
            ("z_suffix", expected),
            ("explicit_offset", anchor.astimezone(timezone(timedelta(hours=2))).isoformat(timespec="seconds")),
        ):
            with self.subTest(format=label):
                with tempfile.TemporaryDirectory() as tmp:
                    app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="900")
                    seed_store(tmp, "dishwasher", {
                        "state": "Running",
                        "cycle_start_time": ts,
                        "expected_dur_at_start": 227,
                    })

                    app.initialize()

                    self.assertEqual(app.state, "Running")
                    self.assertIsNotNone(app.start_time)
                    self.assertIsNotNone(app.start_time.tzinfo)
                    self.assertEqual(cs.format_utc(app.start_time), expected)


class RestoredRunningBelowFinishGuardStaysRunning(unittest.TestCase):
    """Pins today's hard evidence: the ECO programme drew its last measurable power at
    13:17, then sat at 0.0 W for well over two hours while still genuinely running (real end
    ~15:38, passive condensation drying draws nothing). A restored Running at 0 W whose
    elapsed time has not yet reached finish_guard_fraction of the guard duration must stay
    Running - the boot self-heal (dishwasher_monitor.py's own pre-existing code, dead until
    this restore-ordering fix made self.start_time non-None at boot) must NOT finish it early."""

    def test_restored_running_below_finish_guard_with_zero_watts_stays_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(
                tmp,
                sensor_state=None,
                helper_state=None,
                power_w="0",  # passive drying - draws nothing, but still genuinely running
                energy_kwh="10.0",
            )
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                # 180 min < 0.95 * 227 min (~215.65 min) - still short of the finish guard.
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=180)),
                "energy_at_start": "9.60",  # 0.40 kWh used - past min_energy_kwh (0.3)
                "detected_programme": "eco",
                "expected_dur_at_start": 227,
                "max_power_w": 1800.0,
            })

            app.initialize()

            self.assertEqual(app.state, "Running")
            self.assertIsNotNone(app.start_time)
            self.assertIn(app._poll_power, scheduled_callbacks(app))
            self.assertIn(app._running_watchdog_timeout, scheduled_callbacks(app))

    def test_restored_running_past_finish_guard_with_zero_watts_finishes(self):
        """Companion sanity check: once elapsed time clears the finish_guard_fraction
        threshold, the (pre-existing, now finally reachable) boot self-heal DOES route
        through the normal finish path."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(
                tmp,
                sensor_state=None,
                helper_state=None,
                power_w="0",
                energy_kwh="10.0",
            )
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                # 220 min > 0.95 * 227 min (~215.65 min) - past the finish guard.
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=220)),
                "state": "Running",
                "energy_at_start": "9.60",
                "detected_programme": "eco",
                "expected_dur_at_start": 227,
                "max_power_w": 1800.0,
            })

            app.initialize()

            self.assertEqual(app.state, "Unemptied")


class UnemptiedStateSinceSurvivesAcrossRestartsFromTheStore(unittest.TestCase):
    """state_since is the one clock-free-state field the on-disk store exists to carry:
    restoring last_state_change itself is deliberately never done (see its init comment in
    dishwasher_monitor.py - it would let the cooling period swallow the very first
    post-restart transition).

    Unlike the dryer, the dishwasher's Unemptied watchdog is disabled by default
    (unemptied_timeout_hours=0) and initialize() has no restore-time re-arm for it at all
    (unemptied_watchdog_timer is only ever armed from a live _transition_to_unemptied /
    _revert_emptied_to_unemptied, never from initialize()'s restore dispatch) - a pre-existing
    gap, not something this fix introduces or is asked to close. So this pins the mechanism at
    the level the dishwasher actually has: self.state_since must resolve to the store's
    carried-forward 20h-old timestamp, not a fresh "now", which is exactly what a future
    watchdog re-arm (or anything else keyed off "how long has this state been true") would
    need to be correct."""

    def test_20h_old_state_since_is_carried_forward_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            state_since = now - timedelta(hours=20)
            seed_store(tmp, "dishwasher", {
                "state": "Unemptied",
                "state_since": cs.format_utc(state_since),
            })

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertIsNotNone(app.state_since)
            self.assertLessEqual(abs((app.state_since - state_since).total_seconds()), 2)


# ----------------------------------------------------------------------------------------
# FIX 1 (blocker) - 2026-08-12 adversarial review: the boot self-heal used to decide the
# finish-guard duration by calling _get_programme_for_display() -> _classify_programme(), a
# pure function of elapsed+energy - ignoring the detected_programme/expected_dur_at_start
# _restore_cycle_tracking_from_entity had just restored a few lines earlier. The classifier's
# hardcoded ECO band tops out at 0.9 kWh but dishwasher_programmes.yaml rates ECO at 0.95, so
# the tail of a real ECO wash reclassified as "gentle" (149 min) and collapsed the guard from
# 234, forcing a false "ready to empty" up to ~85 min before the wash was actually done - and
# then wrote a short _save_cycle_feedback record that pollutes the learned average too.
#
# Reproduced with the REAL dishwasher_programmes.yaml (not a synthetic test-only one) and
# dishwasher.yaml's actual production thresholds, mirrored below as PRODUCTION_ARGS.
# ----------------------------------------------------------------------------------------

# Numeric/behavioral knobs mirrored by hand from apps/appliances/dishwasher.yaml (entity ids
# and AppDaemon plumbing - module/class/log/dependencies - are supplied separately by
# make_app's own test entities, so are not part of this table). The whole point of
# "production args" here is reproducing the real incident, not a synthetic one, so keep this
# in sync if dishwasher.yaml's tuning ever changes.
PRODUCTION_ARGS = {
    "start_w": 8,
    "stop_w": 2,
    "run_for": 60,
    "stop_for": 90,
    "high_power_threshold": 2,
    "door_close_fast_start_window_s": 900,
    "start_sustain_seconds_without_door": 120,
    "power_unavailable_error_after_seconds": 180,
    "start_validation_window_s": 180,
    "start_validation_min_active_samples": 3,
    "start_validation_fast_confirm_samples": 3,
    "start_validation_fast_confirm_w": 17.0,
    "start_validation_high_confidence_w": 100.0,
    "start_validation_idle_w": 2,
    "start_candidate_idle_grace_s": 25,
    "start_validation_apply_to_recovery": True,
    "start_validation_min_energy_kwh": 0,
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

REAL_PROGRAMMES_FILE = str(Path(__file__).resolve().parents[1] / "dishwasher_programmes.yaml")


class BootSelfHealAnchorsToTheRestoredProgramme(unittest.TestCase):
    """FIX 1: a boot-restored ECO wash sitting on the classifier's energy-band boundary must
    stay Running, anchored to the store's own expected_dur_at_start/detected_programme,
    rather than falling to a fresh (and here, wrong) reclassification."""

    def _run(self, tmp, *, elapsed_min, energy_kwh_used):
        app, entities = make_app(
            tmp,
            sensor_state=None,  # HA erased the entity
            helper_state=None,
            power_w="0.0",  # ECO's passive drying tail - draws nothing, but still running
            energy_kwh="10.0",
            extra_args={**PRODUCTION_ARGS, "programmes_file": REAL_PROGRAMMES_FILE},
        )
        now = app._now_utc()
        seed_store(tmp, "dishwasher", {
            "state": "Running",
            "cycle_start_time": cs.format_utc(now - timedelta(minutes=elapsed_min)),
            "energy_at_start": f"{10.0 - energy_kwh_used:.3f}",
            "detected_programme": "eco",
            "expected_dur_at_start": 234,
            "max_power_w": 1800.0,
        })

        app.initialize()
        return app

    def test_172min_085kwh_classifies_eco_stays_running(self):
        """Control row: even a fresh classify() gets this one right (still inside the
        hardcoded 0.4-0.9 eco band) - confirms the fix does not regress the already-correct
        case."""
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=172, energy_kwh_used=0.85)
            self.assertEqual(app.state, "Running")

    def test_172min_092kwh_stays_running_not_unemptied(self):
        """The exact reproduction: 172 min < 0.95 * 234 (~222.3 min) - must stay Running even
        though a fresh classify() of 0.92 kWh wrongly says "gentle" (149 min guard, already
        cleared at 172 min)."""
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=172, energy_kwh_used=0.92)
            self.assertEqual(app.state, "Running")

    def test_150min_095kwh_stays_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=150, energy_kwh_used=0.95)
            self.assertEqual(app.state, "Running")

    def test_200min_100kwh_stays_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=200, energy_kwh_used=1.00)
            self.assertEqual(app.state, "Running")

    def test_inside_the_dry_tail_defers_instead_of_announcing(self):
        """Guard-open is not the machine's end. At 225 min elapsed the 0.95 * 234 guard
        (~222.3 min) has opened, but eco's dry_tail_minutes: 35 puts the machine's own
        countdown at ~257.3 min - it is still passively drying at 0 W. Finishing here is the
        2026-08-12 mistake in miniature: 'no power' announced as 'come empty me' while the
        display still counts down. The self-heal must defer, not fire."""
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=225, energy_kwh_used=1.00)
            self.assertEqual(app.state, "Running")

    def test_past_guard_plus_dry_tail_still_finishes(self):
        """Companion sanity check (mirrors RestoredRunningBelowFinishGuardStaysRunning above):
        once elapsed clears the guard AND eco's dry tail (~257.3 min), the self-heal must still
        fire - anchoring to the restored programme, and waiting out the tail, does not mean
        never finishing."""
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=265, energy_kwh_used=1.00)
            self.assertEqual(app.state, "Unemptied")


# ----------------------------------------------------------------------------------------
# FIX 2 (blocker) - 2026-08-12 adversarial review: every test above, before this file's
# make_app() was rewritten, stubbed _set_state_entity wholesale, so _build_cycle_store_payload
# / _save_cycle_state / _set_state_entity's own store-write half was executed by zero tests.
# make_app() now stubs only self.set_state (the AppDaemon primitive); the tests below read the
# real on-disk JSON it produced and pin the write side directly.
# ----------------------------------------------------------------------------------------


class StoreWriteReflectsWhatWasActuallyRestored(unittest.TestCase):
    """Every boot path's on-disk store content, read back for real after initialize()."""

    def _load(self, app):
        return cs.CycleStore(app.state_file, "dishwasher").load()

    def test_restored_running_ends_with_a_non_null_cycle_start_time_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="900")
            now = app._now_utc()
            stored_start = now - timedelta(minutes=40)
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(stored_start),
                "expected_dur_at_start": 227,
            })

            app.initialize()

            on_disk = self._load(app)
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.get("state"), "Running")
            self.assertIsNotNone(on_disk.get("cycle_start_time"))
            self.assertEqual(on_disk.get("cycle_start_time"), cs.format_utc(stored_start))

    def test_restored_paused_ends_with_a_non_null_cycle_start_time_on_disk(self):
        """The exact interaction FIX 2 exists to catch: Paused has no _update_running_
        attributes() second write of its own (Running-only), so the on-disk cycle_start_time
        depends entirely on the post-restore store-only save landing - the FIRST write (before
        the restore dispatch runs) necessarily persists it null."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            stored_start = now - timedelta(minutes=40)
            seed_store(tmp, "dishwasher", {
                "state": "Paused",
                "cycle_start_time": cs.format_utc(stored_start),
                "expected_dur_at_start": 227,
            })

            app.initialize()

            self.assertEqual(app.state, "Paused")
            on_disk = self._load(app)
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.get("state"), "Paused")
            self.assertIsNotNone(
                on_disk.get("cycle_start_time"),
                "Paused has no second corrective write of its own - this is the field the "
                "post-restore store-only save exists to fix",
            )
            self.assertEqual(on_disk.get("cycle_start_time"), cs.format_utc(stored_start))

    def test_restored_unemptied_state_persists_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Unemptied",
                "state_since": cs.format_utc(now - timedelta(hours=2)),
            })

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            on_disk = self._load(app)
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.get("state"), "Unemptied")

    def test_restored_emptied_state_persists_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Emptied",
                "state_since": cs.format_utc(now - timedelta(minutes=10)),
            })

            app.initialize()

            self.assertEqual(app.state, "Emptied")
            on_disk = self._load(app)
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.get("state"), "Emptied")

    def test_off_boot_removes_the_store_file(self):
        """A prior boot's stale Running store must not survive a boot that resolves to Off -
        the file must be gone afterward, not merely left with stale content."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(app._now_utc() - timedelta(hours=14)),  # stale
                "expected_dur_at_start": 227,
            })

            app.initialize()

            self.assertEqual(app.state, "Off")
            self.assertFalse(Path(app.state_file).exists())
            self.assertIsNone(self._load(app))


# ----------------------------------------------------------------------------------------
# FIX 3 - 2026-08-12 adversarial review: a store start_time in the future (box clock behind
# real time at boot, before NTP syncs) used to be accepted outright - the "too old" check only
# ever tests age > max, and a negative age trivially passes it.
# ----------------------------------------------------------------------------------------


class FutureStartTimeIsRejected(unittest.TestCase):
    def test_start_time_3h_in_the_future_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now + timedelta(hours=3)),
                "expected_dur_at_start": 227,
            })

            app.initialize()

            self.assertEqual(app.state, "Off")
            warnings = boot_resolution_warnings(app)
            self.assertEqual(len(warnings), 1)
            self.assertIn("future", str(warnings[0][0]).lower())

    def test_small_clock_skew_within_allowance_is_still_accepted(self):
        """A few seconds ahead (wire-format rounding) must not be mistaken for real clock
        skew - only the box's own clock being genuinely behind (NTP not yet synced) should
        reject."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="900")
            now = app._now_utc()
            seed_store(tmp, "dishwasher", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now + timedelta(seconds=5)),
                "expected_dur_at_start": 227,
            })

            app.initialize()

            self.assertEqual(app.state, "Running")


# ----------------------------------------------------------------------------------------
# FIX 4 - 2026-08-12 adversarial review: a helper-seeded Unemptied/Emptied has neither an
# entity last_changed nor a store state_since to anchor to, so state_since stayed None
# forever and every subsequent HA erasure re-armed any watchdog keyed off it from its full
# period.
# ----------------------------------------------------------------------------------------


class HelperSeededUnemptiedGetsAStateSinceAcrossTwoRestarts(unittest.TestCase):
    """Two restarts, same tmpdir (so the second genuinely reads what the first left on disk):
    the first stamps state_since to "now"; the second must carry that SAME stamp forward, not
    stamp yet another fresh "now"."""

    def test_state_since_persists_and_is_not_reset_on_the_second_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            app1, entities1 = make_app(tmp, sensor_state=None, helper_state="Unemptied", power_w="0")
            app1.initialize()

            self.assertEqual(app1.state, "Unemptied")
            self.assertIsNotNone(app1.state_since, "FIX 4: state_since must be stamped, not left None")
            first_stamp = app1.state_since

            # Second restart: fresh app instance, same tmpdir - the entity is STILL erased
            # (HA has not come back), but the store now holds what app1 actually wrote to disk.
            app2, entities2 = make_app(tmp, sensor_state=None, helper_state="Unemptied", power_w="0")
            app2.initialize()

            self.assertEqual(app2.state, "Unemptied")
            self.assertIsNotNone(app2.state_since)
            self.assertLessEqual(
                abs((app2.state_since - first_stamp).total_seconds()), 2,
                "second restart must carry the first restart's stamp forward, not reset it",
            )


if __name__ == "__main__":
    unittest.main()
