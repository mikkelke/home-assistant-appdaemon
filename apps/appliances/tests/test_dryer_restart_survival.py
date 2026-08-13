# tests/test_dryer_restart_survival.py - Restart-survival coverage for the on-disk cycle
# store (cycle_store.py) wired into dryer_monitor.py's initialize().
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# IMPORTANT: unlike test_dryer_unavailable_grace.py / test_dryer_emptied_exit.py, this file
# NEVER stubs _restore_running_state / _restore_unemptied_state / _restore_emptied_state.
# The 2026-07-27 incident this whole feature exists to fix shipped strictly worse than no fix
# BECAUSE its test stubbed out the restore path - precisely the interaction that was broken:
# initialize() seeded state from a helper, _set_state_entity(state=...) recreated the erased
# sensor with no attributes, and the restore path immediately after read cycle_start_time back
# from that same just-created entity, so start_time stayed None forever while state stayed
# Running with no armed exit. Every test below runs the REAL initialize() and the REAL restore
# method it dispatches to, against a faithful get_state() double that distinguishes a bare
# get_state(entity) call from get_state(entity, attribute="all") - the earlier harnesses in
# this directory collapse both to the same bare value, which is exactly why they have to stub
# the restore methods to stay usable.

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

import dryer_monitor as dm  # noqa: E402
import cycle_store as cs  # noqa: E402

STATE_ENTITY = "sensor.dryer_state"
UI_SELECT = "input_select.dryer_state"
POWER_SENSOR = "sensor.dryer_plug_power"
ENERGY_SENSOR = "sensor.dryer_plug_energy"
DOOR_SENSOR = "binary_sensor.dryer_door_contact"


def _entity_get_state(entities, call_log):
    """A get_state() double that actually distinguishes attribute="all" from a bare call -
    the thing the existing harnesses in this directory cannot do, which is why they stub the
    restore methods instead of letting a real boot-attrs read happen.

    Each call_log entry also records the immediate caller's function name (via
    sys._getframe(1), the frame that invoked self.get_state(...) directly - dryer_monitor.py's
    get_state is a plain function assigned straight onto the app instance, not a bound method,
    so there is no intermediate wrapper frame in between) -
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
    """Real DryerMonitor with initialize() run for real - see the module docstring for why
    the restore methods are never stubbed here. Every test-only file (feedback, programmes,
    cycle-state store) lives under `tmpdir` so nothing ever touches, or races on, the real
    apps/appliances/dryer_*.json files.

    _set_state_entity is ALSO never stubbed (FIX 2, 2026-08-12 review): only self.set_state -
    the actual AppDaemon primitive it calls - is faked below, so _save_cycle_state /
    _build_cycle_store_payload / CycleStore.save all run for real on every boot, writing to
    the tmpdir-scoped state_file below (exposed as app.state_file so a test can read back what
    actually landed on disk). Before this fix, every test in this file stubbed
    _set_state_entity wholesale, so that entire write path was exercised by zero tests - the
    same shape of gap the 2026-07-27 incident this suite is named for shipped through."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)

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

    def run_every(cb, start, interval, **kw):
        handle = f"every:{len(app.scheduled)}:{getattr(cb, '__name__', cb)}"
        app.scheduled.append((cb, interval, kw))
        return handle

    app.run_every = run_every
    app.datetime = lambda: app._now_utc()

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
    dryer_monitor.py, not assumed:

    - kind == "get_state" (attribute=None, a bare read): returns the state STRING only - HA/
      AppDaemon never hands back attributes without an explicit attribute= - so this can never
      carry cycle_start_time (which lives exclusively in attributes) regardless of caller. Used
      for plain "what is the published state right now" control-flow checks (_power_changed's
      current_state, the guard at the top of _update_running_attributes, the state= it passes
      through to its own republish, _should_change_state, the *_watchdog_timeout methods) -
      never by a restore function, which always consumes the cached
      _boot_full_state_snapshot()/_boot_store_snapshot() instead of a live call.

    - kind == "get_state_all" (attribute="all") AND caller == "_update_running_attributes": the
      one reachable post-write "all" read, and the dangerous shape in general (this is exactly
      how the original 2026-07-27 bug read cycle_start_time back) - narrowed to this ONE caller
      specifically. Its result seeds a merge dict that is immediately overwritten
      (existing.update(attrs)) by fresh values computed from self.start_time - already restored,
      moments earlier, from the cached snapshot - including cycle_start_time itself, before
      anything is republished. Never assigned to self.start_time or any other restore decision.
      Any OTHER caller doing an "all" read here remains suspect - this does not blanket-allow
      the attribute value.
    """
    kind, _entity, caller = entry
    if kind == "get_state":
        return True
    return kind == "get_state_all" and caller == "_update_running_attributes"


class TheIncidentEndToEnd(unittest.TestCase):
    """The 2026-07-27 incident, reproduced and fixed: a Running cycle in progress when HA
    restarts must come back Running, with its real start_time and both exit timers armed -
    not silently revert to Off, and not sit in Running forever with nothing that can ever end
    it."""

    def test_running_cycle_survives_ha_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            start_time = None
            with_now = None
            app, entities = make_app(
                tmp,
                sensor_state=None,  # HA erased the entity
                helper_state="Off",  # stale mirror value, must not win over the store
                power_w="900",  # comfortably above start_w
            )
            now = app._now_utc()
            stored_start = now - timedelta(minutes=103)
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(stored_start),
                "energy_at_start": "9.0",
                "detected_programme": "bomuld__skabstoert",
                "programme_duration_min": 140,
                "max_power_w": 850.0,
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
                    "detected_programme": "bomuld__skabstoert",
                    "programme_duration_min": 140,
                },
                sensor_last_changed="2026-08-12T09:50:00Z",
                power_w="900",
            )
            seed_store(tmp, "dryer", {
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
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=20)),
                "programme_duration_min": 140,
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
            self.assertIsNotNone(app.unemptied_watchdog_timer)


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
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(hours=14)),
                "programme_duration_min": 140,
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
    lived (a restore function reading the entity back live, post-write, after that same write
    had just recreated it with no attributes). The real invariant is the second one below: no
    restore-relevant get_state(state_entity, ...) call may happen after the FIRST write either -
    every restore function must consume the boot snapshot captured up front. See
    _is_benign_post_write_state_entity_read for the two narrow, specifically-justified
    exceptions actually reachable in this file's boot path - established by reading
    dryer_monitor.py, not assumed, and deliberately not widened to make this test pass."""

    def test_boot_attrs_read_precedes_first_entity_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state="Off", power_w="900")
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=30)),
                "programme_duration_min": 140,
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
    must be armed. (2026-07-27 produced a Running with no armed exit; this pins the opposite.)"""

    def test_every_running_or_paused_boot_path_has_start_time_and_armed_timers(self):
        with tempfile.TemporaryDirectory() as tmp:
            now_ref = dm.DryerMonitor.__new__(dm.DryerMonitor)._now_utc()
        cases = {
            "entity_running": dict(
                sensor_state="Running",
                sensor_attrs={"cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "programme_duration_min": 140},
                helper_state=None,
                store_payload=None,
            ),
            "entity_paused": dict(
                sensor_state="Paused",
                sensor_attrs={"cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "programme_duration_min": 140},
                sensor_last_changed=cs.format_utc(now_ref - timedelta(minutes=2)),
                helper_state=None,
                store_payload=None,
            ),
            "store_running": dict(
                sensor_state=None,
                helper_state=None,
                store_payload={"state": "Running", "cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "programme_duration_min": 140},
            ),
            "store_paused": dict(
                sensor_state=None,
                helper_state=None,
                store_payload={"state": "Paused", "cycle_start_time": cs.format_utc(now_ref - timedelta(minutes=40)), "programme_duration_min": 140},
            ),
        }
        for name, cfg in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    kwargs = {k: v for k, v in cfg.items() if k != "store_payload"}
                    app, entities = make_app(tmp, power_w="900", **kwargs)
                    if cfg.get("store_payload"):
                        seed_store(tmp, "dryer", cfg["store_payload"])

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
            state_path = Path(tmp) / "dryer_cycle_state.json"
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
        anchor = dm.DryerMonitor.__new__(dm.DryerMonitor)._now_utc().replace(microsecond=0) - timedelta(minutes=45)
        expected = cs.format_utc(anchor)
        for label, ts in (
            ("z_suffix", expected),
            ("explicit_offset", anchor.astimezone(timezone(timedelta(hours=2))).isoformat(timespec="seconds")),
        ):
            with self.subTest(format=label):
                with tempfile.TemporaryDirectory() as tmp:
                    app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="900")
                    seed_store(tmp, "dryer", {
                        "state": "Running",
                        "cycle_start_time": ts,
                        "programme_duration_min": 140,
                    })

                    app.initialize()

                    self.assertEqual(app.state, "Running")
                    self.assertIsNotNone(app.start_time)
                    self.assertIsNotNone(app.start_time.tzinfo)
                    self.assertEqual(cs.format_utc(app.start_time), expected)


class BootSelfHealPortedToTheDryer(unittest.TestCase):
    """dishwasher_monitor.py has carried a boot self-heal for a cycle that actually finished
    while AppDaemon/HA was down; the dryer never had one, and its 5h running watchdog only
    ever forces Off (not Unemptied), so a machine that finished during downtime would silently
    lose its empty-me reminder without this. Restored Running, power already low, run time
    past the 80% guard -> the boot self-heal must route it through the normal finish path and
    land on Unemptied, not leave it Running (and definitely not Off)."""

    def test_restored_running_past_guard_with_low_power_ends_unemptied(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(
                tmp,
                sensor_state=None,
                helper_state=None,
                power_w="0",  # already finished - power has dropped
                energy_kwh="10.0",
            )
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=110)),  # past 80% of 120min
                "energy_at_start": "9.5",  # 0.5 kWh used - past min_energy_kwh
                "detected_programme": "unknown",
                "programme_duration_min": 120,
                "max_power_w": 850.0,
            })

            app.initialize()

            self.assertEqual(app.state, "Unemptied")

    def test_restored_running_below_guard_with_low_power_stays_running(self):
        """The companion case: not yet past the guard, so the self-heal must NOT fire - this
        is exactly the passive-drying shape (0 W for a long stretch while genuinely still
        running) the finish guard exists to protect against."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(
                tmp,
                sensor_state=None,
                helper_state=None,
                power_w="0",
                energy_kwh="10.0",
            )
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=50)),  # well under 80% of 120min
                "energy_at_start": "9.5",
                "detected_programme": "unknown",
                "programme_duration_min": 120,
                "max_power_w": 850.0,
            })

            app.initialize()

            self.assertEqual(app.state, "Running")
            self.assertIsNotNone(app.start_time)


# ----------------------------------------------------------------------------------------
# FIX (2026-08-12 review, second pass) - GAP 1: the boot self-heal above used to decide
# guard_dur by calling _classify_programme() (a pure function of live elapsed+energy) and
# _get_confirmed_programme_key() (a live selector read, unconfirmed this early at boot) -
# ignoring self.expected_dur_at_start / self.detected_programme that _restore_running_state had
# just restored a few lines earlier from the store/entity. _classify_programme() can land on a
# SHORTER profile than the one actually restored, collapsing guard_dur and forcing a false
# Unemptied while the dryer is still genuinely running (long low-power cool-down / anti-crease
# tail - watts <= stop_w alone is not proof the cycle ended).
#
# Reproduced with the REAL dryer_programmes.yaml (not a synthetic test-only one): a restored
# "bomuld__skabstoert__skane" cycle (165 min) sitting at 100 min elapsed / 0.90 kWh used
# reclassifies live as "ekspres__skabstoert" (100 min) - already past ITS 80% guard (80 min) -
# while the real 165 min guard's 80% mark (132 min) is nowhere close.
# ----------------------------------------------------------------------------------------

REAL_DRYER_PROGRAMMES_FILE = str(Path(__file__).resolve().parents[1] / "dryer_programmes.yaml")


class BootSelfHealAnchorsToTheRestoredProgramme(unittest.TestCase):
    """A boot-restored long-duration programme sitting at an elapsed time/energy that live
    reclassification would place past ITS OWN (shorter) guard must stay Running, anchored to
    the store's own programme_duration_min/detected_programme - not a fresh (and here, wrong)
    reclassification."""

    def _run(self, tmp, *, elapsed_min, energy_kwh_used):
        app, entities = make_app(
            tmp,
            sensor_state=None,  # HA erased the entity
            helper_state=None,
            power_w="0",  # already at 0W - anti-crease/cool-down tail, or genuinely finished
            energy_kwh="10.0",
            extra_args={"programmes_file": REAL_DRYER_PROGRAMMES_FILE},
        )
        now = app._now_utc()
        seed_store(tmp, "dryer", {
            "state": "Running",
            "cycle_start_time": cs.format_utc(now - timedelta(minutes=elapsed_min)),
            "energy_at_start": f"{10.0 - energy_kwh_used:.3f}",
            "detected_programme": "bomuld__skabstoert__skane",
            "programme_duration_min": 165,
            "max_power_w": 850.0,
        })

        app.initialize()
        return app

    def test_100min_090kwh_stays_running_not_unemptied(self):
        """The exact reproduction: 100 min < 0.8 * 165 (132 min) - must stay Running even
        though a fresh classify() of 0.90 kWh at 100 min wrongly says "ekspres__skabstoert"
        (100 min guard -> 80 min 80%-mark, already cleared at 100 min elapsed)."""
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=100, energy_kwh_used=0.90)
            self.assertEqual(app.state, "Running")
            self.assertIsNotNone(app.start_time)

    def test_past_the_restored_guard_still_finishes(self):
        """Companion sanity check: anchoring to the restored programme does not mean never
        finishing - once elapsed clears 80% of the RESTORED (165 min) guard, the self-heal must
        still fire and land on Unemptied (the empty-me reminder it exists to preserve), not
        Off."""
        with tempfile.TemporaryDirectory() as tmp:
            app = self._run(tmp, elapsed_min=140, energy_kwh_used=1.70)
            self.assertEqual(app.state, "Unemptied")


class UnemptiedStateSinceSurvivesAcrossRestartsForTheWatchdog(unittest.TestCase):
    """state_since is the one clock-free-state field the on-disk store exists to carry:
    restoring last_state_change itself is deliberately never done (see its init comment in
    dryer_monitor.py - it would let the cooling period swallow the very first post-restart
    transition), so state_since is what lets the 24h Unemptied watchdog re-arm with the
    correct remaining time across a SECOND restart, instead of a fresh 24h every time."""

    def test_20h_old_state_since_arms_the_watchdog_with_roughly_4h_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            state_since = now - timedelta(hours=20)
            seed_store(tmp, "dryer", {
                "state": "Unemptied",
                "state_since": cs.format_utc(state_since),
            })

            app.initialize()

            self.assertEqual(app.state, "Unemptied")
            self.assertIsNotNone(app.unemptied_watchdog_timer)
            delay = next(
                d for cb, d, _kw in app.scheduled if cb == app._unemptied_watchdog_timeout
            )
            # 24h default unemptied_timeout_hours - 20h already elapsed = ~4h remaining, not a
            # fresh 24h.
            self.assertAlmostEqual(delay, 4 * 3600, delta=5)


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
        return cs.CycleStore(app.state_file, "dryer").load()

    def test_restored_running_ends_with_a_non_null_cycle_start_time_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="900")
            now = app._now_utc()
            stored_start = now - timedelta(minutes=40)
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(stored_start),
                "programme_duration_min": 140,
            })

            app.initialize()

            on_disk = self._load(app)
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.get("state"), "Running")
            self.assertIsNotNone(on_disk.get("cycle_start_time"))
            self.assertEqual(on_disk.get("cycle_start_time"), cs.format_utc(stored_start))

    def test_restored_paused_ends_with_a_non_null_cycle_start_time_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            stored_start = now - timedelta(minutes=40)
            seed_store(tmp, "dryer", {
                "state": "Paused",
                "cycle_start_time": cs.format_utc(stored_start),
                "programme_duration_min": 140,
            })

            app.initialize()

            self.assertEqual(app.state, "Paused")
            on_disk = self._load(app)
            self.assertIsNotNone(on_disk)
            self.assertEqual(on_disk.get("state"), "Paused")
            self.assertIsNotNone(on_disk.get("cycle_start_time"))
            self.assertEqual(on_disk.get("cycle_start_time"), cs.format_utc(stored_start))

    def test_restored_unemptied_state_persists_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, sensor_state=None, helper_state=None, power_w="0")
            now = app._now_utc()
            seed_store(tmp, "dryer", {
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
            seed_store(tmp, "dryer", {
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
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(app._now_utc() - timedelta(hours=14)),  # stale
                "programme_duration_min": 140,
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
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now + timedelta(hours=3)),
                "programme_duration_min": 140,
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
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now + timedelta(seconds=5)),
                "programme_duration_min": 140,
            })

            app.initialize()

            self.assertEqual(app.state, "Running")


# ----------------------------------------------------------------------------------------
# FIX 4 - 2026-08-12 adversarial review: a helper-seeded Unemptied/Emptied has neither an
# entity last_changed nor a store state_since to anchor to, so state_since stayed None
# forever and every subsequent HA erasure re-armed the Unemptied/Emptied watchdog from its
# full period - with six HA restarts in one day, as happened today, the auto-clear then never
# fires at all.
# ----------------------------------------------------------------------------------------


class HelperSeededUnemptiedGetsAStateSinceAcrossTwoRestarts(unittest.TestCase):
    """Two restarts, same tmpdir (so the second genuinely reads what the first left on disk):
    the first stamps state_since to "now"; the second must carry that SAME stamp forward, not
    stamp yet another fresh "now" - and must therefore re-arm the 24h Unemptied watchdog with
    time already deducted, not a fresh 24h."""

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

    def test_second_restart_watchdog_has_time_already_deducted_not_a_fresh_24h(self):
        """The observable consequence for the dryer specifically: _restore_unemptied_state
        re-arms the 24h auto-clear from state_since - if FIX 4 did not hold, state_since would
        stay None forever and could never distinguish "just became Unemptied" from "been
        Unemptied for 20 hours", so every restart would re-arm a fresh 24h regardless. Restart
        1 proves FIX 4 populates state_since at all; back-dating what it actually wrote (rather
        than seeding a synthetic payload) and restarting again proves the watchdog re-arm
        consumes it correctly end to end - mirrors
        UnemptiedStateSinceSurvivesAcrossRestartsForTheWatchdog above, but state_since here
        originates from FIX 4's fallback stamp, not a directly-seeded store payload."""
        with tempfile.TemporaryDirectory() as tmp:
            app1, entities1 = make_app(tmp, sensor_state=None, helper_state="Unemptied", power_w="0")
            app1.initialize()
            self.assertEqual(app1.state, "Unemptied")

            store = cs.CycleStore(app1.state_file, "dryer")
            payload = store.load()
            self.assertIsNotNone(payload)
            self.assertIsNotNone(payload.get("state_since"), "FIX 4: restart 1 must have written a real state_since")
            backdated = app1._now_utc() - timedelta(hours=20)
            payload["state_since"] = cs.format_utc(backdated)
            self.assertTrue(store.save(payload))

            app2, entities2 = make_app(tmp, sensor_state=None, helper_state="Unemptied", power_w="0")
            app2.initialize()

            self.assertEqual(app2.state, "Unemptied")
            delay = next(
                d for cb, d, _kw in app2.scheduled if cb == app2._unemptied_watchdog_timeout
            )
            # 24h default unemptied_timeout_hours - 20h already elapsed = ~4h remaining, not a
            # fresh 24h.
            self.assertAlmostEqual(delay, 4 * 3600, delta=5)


class TheRealUnloadAfterRestartKeepsTheReminder(unittest.TestCase):
    """Replay of an observed loss, 2026-08-12 17:33-18:12 (old code, box logs).

    A dryer had been running for hours when HA restarted. The entity was erased, the app fell
    to Off, and power re-detection then started a FRESH clock at 17:34. When the load was
    actually taken out at 18:12, that fresh clock read only 37 min - inside fill_window_minutes
    (60) - so the unload was misread as "add laundry":

        Door opened with low power before 60min - Off (add laundry/interrupted)
        State -> Off (Door opened before fill window - add laundry or interrupted)

    No empty-me reminder, and the cycle was recorded as interrupted. Removing the helper's
    `initial: 'Off'` was NOT sufficient here: the helper carried "Running" correctly by then,
    but a helper has no clock, so the old code still refused to seed it. Only the durable
    store's clock makes the door-open read as the unload it actually was.
    """

    def test_restored_clock_turns_the_unload_into_unemptied_not_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Energy meter reads 10.0 kWh; the real ~3h cycle had consumed ~1.5, so it began
            # at 8.5 - without a persisted energy_at_start the cycle fails _is_valid_completed_
            # cycle's energy floor and lands on Off for an entirely different reason.
            app, entities = make_app(
                tmp, sensor_state=None, helper_state="Running", power_w="300", energy_kwh="10.0",
            )
            now = app._now_utc()
            seed_store(tmp, "dryer", {
                "state": "Running",
                "cycle_start_time": cs.format_utc(now - timedelta(minutes=185)),
                "energy_at_start": 8.5,
                "detected_programme": "bomuld__ekstra_toert",
                "programme_duration_min": 165,
            })

            app.initialize()
            self.assertEqual(app.state, "Running")
            self.assertAlmostEqual(app._get_run_duration_minutes(), 185, delta=1)

            # The human opens the door to unload, exactly as at 18:12.
            entities[POWER_SENSOR]["state"] = "0.0"
            app._door_state_changed(DOOR_SENSOR, None, "off", "on", {})

            # Unemptied then Emptied is the correct front-loader path; the bug produced Off.
            self.assertIn(app.state, ("Unemptied", "Emptied"))
            self.assertFalse(
                any("add laundry" in str(a[0]) for a, _kw in app.log_calls),
                "the unload must not be misread as an add-laundry interruption again",
            )


if __name__ == "__main__":
    unittest.main()
