# tests/test_intercom.py - unit tests for Intercom's pure/persistence surface
# (resume_decision, the ring-state save/load helpers, and _handle_trigger's
# clean-edge-vs-replay-edge branching) - mirrors test_lock_health.py's
# stub-and-import pattern.
# Run from repo root: python3 -m unittest discover -s apps/intercom/tests -q
# Imports the real module by stubbing the appdaemon package (not installed locally),
# so the code under test is the deployed code, not a duplicate.

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Stub appdaemon.plugins.hass.hassapi before importing the app module.
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for name, mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(name, mod)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import intercom  # noqa: E402

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


class ResumeDecisionTests(unittest.TestCase):
    def test_succeeded_discards_regardless_of_age(self):
        self.assertEqual(
            intercom.resume_decision({"succeeded": True}, age_s=5, max_age_s=60), "discard_succeeded")
        self.assertEqual(
            intercom.resume_decision({"succeeded": True}, age_s=99999, max_age_s=60), "discard_succeeded")

    def test_missing_succeeded_key_defaults_falsy(self):
        self.assertEqual(intercom.resume_decision({}, age_s=10, max_age_s=60), "alert")

    def test_negative_age_is_stale(self):
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=-1, max_age_s=60), "discard_stale")

    def test_within_window_is_alert(self):
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=0, max_age_s=60), "alert")
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=30, max_age_s=60), "alert")
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=60, max_age_s=60), "alert")  # boundary

    def test_over_max_age_is_stale(self):
        self.assertEqual(intercom.resume_decision({"succeeded": False}, age_s=61, max_age_s=60), "discard_stale")


def _bare_intercom(state_file):
    """A bare, un-initialized Intercom instance (bypasses initialize()/AD wiring
    entirely, via __new__ - hass.Hass is stubbed to `object` above) with just
    _state_file set - enough to exercise the ring-persistence helpers below, which
    only ever touch self._state_file and self.log."""
    app = intercom.Intercom.__new__(intercom.Intercom)
    app._state_file = state_file
    app.log = lambda *a, **kw: None
    return app


class RingStateRoundTripTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.state_file = Path(tmpdir.name) / "intercom_state.json"
        self.app = _bare_intercom(self.state_file)

    def test_missing_file_loads_empty_dict(self):
        self.assertEqual(self.app._load_ring_state(), {})

    def test_save_then_load_round_trip(self):
        records = {"binary_sensor.front": {"ring_ts": NOW.isoformat(), "ring_label": "front door",
                                            "lock_entity": "lock.front", "succeeded": False}}
        self.app._save_ring_state(records)
        self.assertEqual(self.app._load_ring_state(), records)

    def test_save_is_atomic_no_leftover_tmp_file(self):
        self.app._save_ring_state({"a": 1})
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        self.assertTrue(self.state_file.exists())
        self.assertFalse(tmp.exists())

    def test_persist_ring_writes_expected_schema(self):
        self.app._persist_ring("binary_sensor.front", NOW, "front door", "lock.front")
        on_disk = json.loads(self.state_file.read_text())
        self.assertEqual(on_disk, {
            "binary_sensor.front": {
                "ring_ts": NOW.isoformat(),
                "ring_label": "front door",
                "lock_entity": "lock.front",
                "succeeded": False,
            }
        })

    def test_mark_ring_succeeded_updates_existing_record_only(self):
        self.app._persist_ring("binary_sensor.front", NOW, "front door", "lock.front")
        self.app._mark_ring_succeeded("binary_sensor.front")
        self.assertTrue(self.app._load_ring_state()["binary_sensor.front"]["succeeded"])
        # No record for this entity yet - must not fabricate one.
        self.app._mark_ring_succeeded("binary_sensor.back")
        self.assertNotIn("binary_sensor.back", self.app._load_ring_state())

    def test_forget_ring_deletes_and_is_idempotent(self):
        self.app._persist_ring("binary_sensor.front", NOW, "front door", "lock.front")
        self.app._forget_ring("binary_sensor.front")
        self.assertEqual(self.app._load_ring_state(), {})
        self.app._forget_ring("binary_sensor.front")  # second call: no-op, must not raise
        self.assertEqual(self.app._load_ring_state(), {})

    def test_parse_ts_round_trip_and_unparseable(self):
        self.assertEqual(intercom.Intercom._parse_ts(NOW.isoformat()), NOW)
        self.assertIsNone(intercom.Intercom._parse_ts(None))
        self.assertIsNone(intercom.Intercom._parse_ts(""))
        self.assertIsNone(intercom.Intercom._parse_ts("not-a-timestamp"))


def _fake_handle_trigger_app(state_file, states):
    """A bare Intercom instance with the minimal duck-typed AD surface
    _handle_trigger touches, so its clean_edge/replay-edge branching can be
    exercised without a real AppDaemon runtime. run_in is captured, not executed -
    this does not exercise _perform_unlock/_verify_unlock or the real timer
    lifecycle, only what _handle_trigger itself decides to schedule/persist."""
    app = intercom.Intercom.__new__(intercom.Intercom)
    app._state_file = state_file
    app.trigger_map = {
        "binary_sensor.front": {
            "message": "Someone is at the front door",
            "lock": "lock.front",
            "followup": "I opened the front door",
            "door_sensor": None,
            "ring_label": "front door",
        }
    }
    app.last_trigger_at = {}
    app.pending_unlocks = {}
    app.unlock_outcomes = {}
    app.abb_unlock_doors = {}  # unconfigured (default) - _handle_trigger's ABB branch must be a no-op
    app.voice_before_unlock = {}  # unconfigured: unlock dispatches immediately
    app.voice_before_unlock_message = "Door is opening."
    app.voice_before_unlock_tts = "tts.piper"
    app.voice_before_unlock_wait_s = 2.5
    app.auto_open_entity = "input_boolean.auto_open"
    app.debounce_s = 5
    app.unlock_delay_s = 1
    app.unlock_repeat_count = 2
    app.unlock_repeat_interval_s = 7
    app.sonos_notifier = None  # skip TTS/submit_to_executor entirely - not under test here
    app.run_in_calls = []

    app.get_now = lambda: NOW
    app.log = lambda *a, **kw: None
    app.get_state = lambda entity, attribute=None: states.get(entity)
    app.fire_event = lambda *a, **kw: None
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None

    def fake_run_in(callback, delay, **kwargs):
        app.run_in_calls.append((delay, kwargs))
        return f"handle-{len(app.run_in_calls)}"
    app.run_in = fake_run_in
    return app


class HandleTriggerEdgeTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.state_file = Path(tmpdir.name) / "intercom_state.json"
        self.states = {"input_boolean.auto_open": "on", "lock.front": "locked"}
        self.app = _fake_handle_trigger_app(self.state_file, self.states)

    def test_clean_edge_schedules_unlocks_and_persists(self):
        self.app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        self.assertEqual(len(self.app.run_in_calls), self.app.unlock_repeat_count)
        on_disk = json.loads(self.state_file.read_text())
        self.assertIn("binary_sensor.front", on_disk)
        self.assertFalse(on_disk["binary_sensor.front"]["succeeded"])

    def test_replay_edge_never_schedules_unlocks_or_persists(self):
        self.app._handle_trigger("binary_sensor.front", "state", "unavailable", "on", {})
        self.assertEqual(self.app.run_in_calls, [])
        self.assertFalse(self.state_file.exists())

    def test_unknown_to_on_is_also_a_replay_edge(self):
        self.app._handle_trigger("binary_sensor.front", "state", "unknown", "on", {})
        self.assertEqual(self.app.run_in_calls, [])
        self.assertFalse(self.state_file.exists())

    def test_non_on_new_state_is_ignored_entirely(self):
        self.app._handle_trigger("binary_sensor.front", "state", "on", "off", {})
        self.assertEqual(self.app.run_in_calls, [])
        self.assertFalse(self.state_file.exists())


# ---------------------------------------------------------------------------
# ABB-native unlock experiment (see abb_unlock_doors in intercom.yaml).
#
# Unlike _fake_handle_trigger_app above (which only inspects what
# _handle_trigger decides to schedule/persist), these tests need to actually
# fire the captured run_in callbacks - the ack watchdog's timeout, and the ESP
# ladder's unlock_callback/_verify_unlock chain it can fall back to - so the
# fixture mirrors test_abb_welcome_bridge.py's Clock + capture-and-replay
# run_in pattern instead.
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self, at):
        self.at = at

    def now(self):
        return self.at


def _run_scheduled(app, callback_name):
    """Run (and consume) every currently-captured run_in call bound to the
    given function name - mirrors test_abb_welcome_bridge.py's helper of the
    same name/contract. A callback that schedules more work of a DIFFERENT
    name (e.g. the timeout scheduling the ESP ladder) lands in run_in_calls
    for a later pass, not this one."""
    pending = app.run_in_calls
    app.run_in_calls = []
    ran = 0
    remaining = []
    for callback, delay, kwargs in pending:
        if getattr(callback, "__name__", "") == callback_name:
            callback(kwargs)
            ran += 1
        else:
            remaining.append((callback, delay, kwargs))
    app.run_in_calls = remaining + app.run_in_calls
    return ran


def _fake_abb_app(tmpdir, clock, states, abb_unlock_doors=None, voice_before_unlock=None):
    """A bare Intercom instance with the full duck-typed AD surface the
    ABB-native unlock path touches: run_in is captured AND replayable (see
    _run_scheduled), submit_to_executor runs inline (so _press_abb_button's
    call_service is recorded synchronously), and call_service/create_task are
    recorded - enough to exercise the ring path, the ack-timeout ESP fallback,
    and the external-press watchdog end to end."""
    app = intercom.Intercom.__new__(intercom.Intercom)
    tmp = Path(tmpdir)
    app._state_file = tmp / "intercom_state.json"
    app.trigger_map = {
        "binary_sensor.front": {
            "message": "Someone is at the front door",
            "lock": "lock.front",
            "followup": "I opened the front door",
            "door_sensor": None,
            "ring_label": "front door",
        }
    }
    app.door_lock_info = {"front door": {"lock": "lock.front", "door_sensor": None}}
    app.abb_unlock_doors = dict(abb_unlock_doors or {})
    app.abb_button_to_door = {v: k for k, v in app.abb_unlock_doors.items()}
    app.abb_unlock_ack_timeout_s = 2.5
    app.voice_before_unlock = dict(voice_before_unlock or {})
    app.voice_before_unlock_message = "Door is opening."
    app.voice_before_unlock_tts = "tts.piper"
    app.voice_before_unlock_wait_s = 2.5
    app._abb_watchdogs = {}
    app._own_abb_press_at = {}

    app.last_trigger_at = {}
    app.pending_unlocks = {}
    app.unlock_outcomes = {}
    app.auto_open_entity = "input_boolean.auto_open"
    app.debounce_s = 5
    app.unlock_delay_s = 1
    app.unlock_repeat_count = 2
    app.unlock_repeat_interval_s = 7
    app.sonos_notifier = None  # skip TTS - not under test here
    app.notify_target = "mikkel"
    app.abb_bridge = None

    app.get_now = clock.now
    app.logs = []
    app.log = lambda msg, level="INFO": app.logs.append((level, msg))
    app.get_state = lambda entity, attribute=None: states.get(entity)
    app.fire_event = lambda *a, **kw: None
    app.timer_running = lambda handle: False
    app.cancel_timer = lambda handle: None

    app.run_in_calls = []

    def fake_run_in(callback, delay, **kwargs):
        app.run_in_calls.append((callback, delay, kwargs))
        return f"handle-{len(app.run_in_calls)}"
    app.run_in = fake_run_in

    def inline_executor(fn, *args, **kwargs):
        fn(*args, **kwargs)
    app.submit_to_executor = inline_executor

    app.service_calls = []
    app.call_service = lambda service, **kwargs: app.service_calls.append((service, kwargs))

    app.pushes = []

    class FakeNotifier:
        def notify(self, **kwargs):  # plain callable: Intercom wraps it in create_task
            app.pushes.append(kwargs)
            return None
    app.mobile_notifier = FakeNotifier()
    app.created_tasks = []
    app.create_task = lambda x: app.created_tasks.append(x)
    return app


class AbbNativeUnlockTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmpdir = tmpdir.name
        self.clock = Clock(NOW)
        self.states = {"input_boolean.auto_open": "on", "lock.front": "locked"}

    def _app(self, abb_unlock_doors=None, voice_before_unlock=None):
        return _fake_abb_app(self.tmpdir, self.clock, self.states, abb_unlock_doors, voice_before_unlock)

    # --- voice_before_unlock: ring -> recording -> announcement -> unlock ---

    def test_voice_before_unlock_defers_the_unlock_and_speaks_first(self):
        """The visitor is only in frame until the door opens, and this gateway
        needs ~2s for a first video frame while the unlock fires at ~+0.45s -
        so opening immediately guarantees an empty clip."""
        app = self._app(
            {"front door": "button.abb_front"},
            voice_before_unlock={"front door": "camera.abb_front"},
        )
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})

        # Spoke, and did NOT press the button yet.
        self.assertTrue(
            any(c[0] == "abb_welcome/play_audio" for c in app.service_calls),
            "should have spoken before unlocking",
        )
        self.assertEqual(
            [c for c in app.service_calls if c[0] == "button/press"], []
        )
        self.assertIn("_deferred_unlock", [cb.__name__ for cb, _, _ in app.run_in_calls])

        # ...and the unlock happens once the wait elapses.
        _run_scheduled(app, "_deferred_unlock")
        self.assertEqual(
            [c for c in app.service_calls if c[0] == "button/press"],
            [("button/press", {"entity_id": "button.abb_front"})],
        )

    def test_unlock_still_happens_when_the_voice_is_rejected(self):
        """SAFETY: the announcement can never gate the door.

        play_audio has been rejected outright and has sent zero packets on live
        rings, so a voice failure must cost the wait and nothing else.
        """
        app = self._app(
            {"front door": "button.abb_front"},
            voice_before_unlock={"front door": "camera.abb_front"},
        )

        def reject(service, **kwargs):
            app.service_calls.append((service, kwargs))
            if service == "abb_welcome/play_audio":
                return {"success": False, "error": {"code": "x", "message": "in use"}}
            return None

        app.call_service = reject
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        _run_scheduled(app, "_deferred_unlock")
        self.assertEqual(
            [c for c in app.service_calls if c[0] == "button/press"],
            [("button/press", {"entity_id": "button.abb_front"})],
        )

    def test_unlock_still_happens_when_the_voice_raises(self):
        """A hung or throwing announcement must not leave the door shut."""
        app = self._app(
            {"front door": "button.abb_front"},
            voice_before_unlock={"front door": "camera.abb_front"},
        )

        def boom(service, **kwargs):
            if service == "abb_welcome/play_audio":
                raise RuntimeError("tts exploded")
            app.service_calls.append((service, kwargs))

        app.call_service = boom
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        _run_scheduled(app, "_deferred_unlock")
        self.assertEqual(
            [c for c in app.service_calls if c[0] == "button/press"],
            [("button/press", {"entity_id": "button.abb_front"})],
        )

    def test_unconfigured_door_unlocks_immediately_as_before(self):
        """No voice configured -> unchanged behaviour, no deferral."""
        app = self._app({"front door": "button.abb_front"})
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        self.assertEqual(
            [c for c in app.service_calls if c[0] == "button/press"],
            [("button/press", {"entity_id": "button.abb_front"})],
        )
        self.assertNotIn(
            "_deferred_unlock", [cb.__name__ for cb, _, _ in app.run_in_calls]
        )

    def test_abb_ack_in_time_succeeds_without_esp_unlock(self):
        app = self._app({"front door": "button.abb_front"})
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})

        # Pressed the ABB button (not the ESP lock), armed exactly the ack
        # watchdog, and scheduled NO ESP unlock_callback attempts.
        self.assertEqual(app.service_calls, [("button/press", {"entity_id": "button.abb_front"})])
        self.assertIn("front door", app._abb_watchdogs)
        scheduled = [cb.__name__ for cb, _, _ in app.run_in_calls]
        self.assertEqual(scheduled, ["on_timeout"])

        # ESP lock reports the physical ack 0.8s later.
        self.clock.at = NOW + timedelta(seconds=0.8)
        self.states["lock.front"] = "unlocking"
        app._on_esp_lock_ack("lock.front", "state", "locked", "unlocking", {"door_label": "front door"})

        self.assertNotIn("front door", app._abb_watchdogs)
        self.assertTrue(any("ABB-UNLOCK ok door=front door" in msg for _, msg in app.logs))
        self.assertEqual(len(app.pushes), 1)
        self.assertEqual(app.pushes[0]["title"], "Intercom auto-opened")
        on_disk = json.loads((Path(self.tmpdir) / "intercom_state.json").read_text())
        self.assertTrue(on_disk["binary_sensor.front"]["succeeded"])

        # A later ack for the same, already-resolved watchdog must not double-report.
        app._on_esp_lock_ack("lock.front", "state", "unlocking", "unlocked", {"door_label": "front door"})
        self.assertEqual(len(app.pushes), 1)
        # The ESP ladder itself was never touched.
        self.assertEqual(app.service_calls, [("button/press", {"entity_id": "button.abb_front"})])

    def test_abb_ack_timeout_falls_back_to_esp_success_reports_once(self):
        app = self._app({"front door": "button.abb_front"})
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        self.assertEqual(_run_scheduled(app, "on_timeout"), 1)
        self.assertTrue(any(
            level == "WARNING" and "ABB-UNLOCK timeout door=front door" in msg for level, msg in app.logs
        ))
        self.assertNotIn("front door", app._abb_watchdogs)

        # Falls through to the UNCHANGED ESP ladder: unlock_repeat_count attempts.
        scheduled = [cb.__name__ for cb, _, _ in app.run_in_calls]
        self.assertEqual(scheduled, ["unlock_callback"] * app.unlock_repeat_count)

        _run_scheduled(app, "unlock_callback")
        self.assertIn(("lock/unlock", {"entity_id": "lock.front"}), app.service_calls)
        self.states["lock.front"] = "unlocking"  # ESP's physical ack, before either verify runs
        _run_scheduled(app, "_verify_unlock")

        titles = [p["title"] for p in app.pushes]
        self.assertEqual(titles, ["Intercom auto-opened"])  # exactly once, not once per attempt

    def test_abb_ack_timeout_then_esp_failure_reports_once(self):
        app = self._app({"front door": "button.abb_front"})
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        _run_scheduled(app, "on_timeout")
        _run_scheduled(app, "unlock_callback")
        # Lock state never changes - every attempt fails to verify.
        _run_scheduled(app, "_verify_unlock")

        titles = [p["title"] for p in app.pushes]
        self.assertEqual(titles, ["Intercom auto-open failed"])  # exactly once

    def test_unconfigured_abb_unlock_doors_is_a_no_op(self):
        app = self._app(abb_unlock_doors={})
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})

        self.assertEqual(app.service_calls, [])  # no ABB button ever pressed
        self.assertEqual(app._abb_watchdogs, {})
        scheduled = [cb.__name__ for cb, _, _ in app.run_in_calls]
        self.assertEqual(scheduled, ["unlock_callback"] * app.unlock_repeat_count)

    def test_external_press_arms_watchdog_and_falls_back(self):
        app = self._app({"front door": "button.abb_front"})
        app._on_abb_button_state("button.abb_front", "state", "2020-01-01T00:00:00+00:00", NOW.isoformat(), {})

        self.assertIn("front door", app._abb_watchdogs)
        self.assertIsNone(app._abb_watchdogs["front door"]["trigger_entity"])
        scheduled = [cb.__name__ for cb, _, _ in app.run_in_calls]
        self.assertEqual(scheduled, ["on_timeout"])
        self.assertTrue(any(
            "ABB-UNLOCK external press detected door=front door" in msg for _, msg in app.logs
        ))

        _run_scheduled(app, "on_timeout")
        self.assertNotIn("front door", app._abb_watchdogs)
        scheduled = [cb.__name__ for cb, _, _ in app.run_in_calls]
        self.assertEqual(scheduled, ["unlock_callback"] * app.unlock_repeat_count)

        # No ring context - the ESP fallback runs, but there is no ring to
        # report on, success or failure.
        _run_scheduled(app, "unlock_callback")
        self.states["lock.front"] = "unlocking"
        _run_scheduled(app, "_verify_unlock")
        self.assertEqual(app.pushes, [])

    def test_own_press_does_not_arm_second_watchdog(self):
        app = self._app({"front door": "button.abb_front"})
        app._handle_trigger("binary_sensor.front", "state", "off", "on", {})
        self.assertEqual(len(app.run_in_calls), 1)  # the one watchdog _start_abb_unlock armed

        # The state change our own press causes arrives back at the button entity.
        app._on_abb_button_state("button.abb_front", "state", "unknown", NOW.isoformat(), {})

        self.assertEqual(len(app.run_in_calls), 1)  # unchanged - no second watchdog armed

    def test_stale_button_timestamp_is_ignored(self):
        app = self._app({"front door": "button.abb_front"})
        stale = (NOW - timedelta(hours=2)).isoformat()
        app._on_abb_button_state("button.abb_front", "state", "unknown", stale, {})

        self.assertEqual(app._abb_watchdogs, {})
        self.assertEqual(app.run_in_calls, [])
        self.assertTrue(any("stale" in msg.lower() for _, msg in app.logs))


if __name__ == "__main__":
    unittest.main()
