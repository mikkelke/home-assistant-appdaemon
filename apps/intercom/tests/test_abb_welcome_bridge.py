# tests/test_abb_welcome_bridge.py - unit tests for the ABB Welcome bridge
# (missed-call suppression/debounce, ring comparator counting + lag math, the
# ring image archive, and the strictly-additive intercom.py attachment seam).
# Mirrors test_intercom.py's stub-and-import pattern: the appdaemon package is
# stubbed, the REAL modules are imported, and instances are built bare via
# __new__ with a duck-typed AD surface - so the code under test is the deployed
# code, not a duplicate.
# Run from repo root: python3 -m unittest discover -s apps/intercom/tests -q
#
# The missed-call fixtures replay the REAL 2026-08-12 14:24 back-door ring
# (auto-open answered it, ABB still logged "Call Missed") verified from HA
# history while building this app - see abb_welcome_bridge.py's header.

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Stub appdaemon.plugins.hass.hassapi before importing the app modules.
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
import abb_welcome_bridge as bridge_mod  # noqa: E402
import intercom  # noqa: E402

# The real 2026-08-12 14:24 back-door ring, from HA history (UTC):
RING_ESP = datetime(2026, 8, 12, 14, 24, 44, 256000, tzinfo=timezone.utc)
RING_ABB = datetime(2026, 8, 12, 14, 24, 44, 203000, tzinfo=timezone.utc)  # SIP led by ~53 ms
UNLOCK_AT = datetime(2026, 8, 12, 14, 24, 53, 222000, tzinfo=timezone.utc)  # lock "unlocking", ring+9.0 s
MISSED_TS = "2026-08-12T14:24:47+00:00"  # portal call-missed timestamp
MISSED_ARRIVED = datetime(2026, 8, 12, 14, 24, 51, 337000, tzinfo=timezone.utc)  # cloud poll delivery
MISSED_ID = "2b7cecce-a3a7-4575-a8ee-d0b4bff35e2f"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64


class Clock:
    def __init__(self, at):
        self.at = at

    def now(self):
        return self.at


def _bare_bridge(tmpdir, clock):
    """A bare, un-initialized AbbWelcomeBridge (bypasses initialize()/AD wiring
    via __new__) with the full duck-typed AD surface the callbacks touch.
    run_in is captured (not executed); submit_to_executor runs INLINE so the
    executor paths are exercised synchronously; create_task captures pushes."""
    app = bridge_mod.AbbWelcomeBridge.__new__(bridge_mod.AbbWelcomeBridge)
    tmp = Path(tmpdir)

    # knobs (mirroring abb_welcome_bridge.yaml defaults)
    app.abb_event_entity = "event.abb"
    app.abb_ringing_sensor = "binary_sensor.abb_ringing"
    app.abb_image_entity = "image.abb_screenshot"
    app.abb_ring_bus_event = "abb_welcome_ring"
    app.esp_door_sensors = {
        "binary_sensor.intercomproxy_doorbell_front_door": "front door",
        "binary_sensor.intercomproxy_doorbell_back_door": "back door",
    }
    app.esp_lock_doors = {
        "lock.intercomproxy_front_door": "front door",
        "lock.intercomproxy_back_door": "back door",
    }
    app.auto_open_entity = "input_boolean.auto_open_intercom"
    app.station_doors = {"100000001": "back door", "100000002": "front door"}
    app.pair_window_s = 10
    app.debounce_s = 5
    app.episode_close_s = 75
    app.suppress_window_s = 30
    app.missed_decision_delay_s = 15
    app.missed_max_age_s = 900
    app.snapshot_probe_s = 6
    app.notify_target = "home"
    app.archive_dir = tmp / "abb_doorbell"
    app.archive_url_prefix = "/local/abb_doorbell"
    app.retain_days = 90
    app.max_files = 2000
    app.archive_warn_mb = 500

    # state
    app.episodes = {}
    app._episode_seq = 0
    app.unlock_events = {}
    app._last_announce_at = {}
    app.door_open_feed = True
    app.door_open_fold_s = 20
    app.door_open_ring_window_s = 90
    app._last_door_open_edge_at = {}
    app._last_ring_closed_at = {}
    app.ring_push_photo_wait_s = 30.0
    app.announce_cameras = {"front door": "camera.abb_front"}
    app.announce_message = "The door is open."
    app.announce_cooldown_s = 90
    app.voice_tts_entity = "tts.piper"
    app.voice_start_delay_s = 9.0
    app.announce_ring_window_s = 60
    app.clip_cameras = {"front door": "camera.abb_front"}
    app.clip_seconds = 10
    app.clip_delay_s = 0
    app.native_ring_clips = False
    app.clip_record_dir = "/config/www/abb_doorbell"
    app.station_by_door = {"back door": "100000001", "front door": "100000002"}
    app.health_sip_entity = "sensor.abb_sip"
    app.health_unhealthy_s = 600
    app.health_heal_cooldown_s = 3600
    app.health_notify = ["mikkel"]
    app._health_bad_since = None
    app._health_last_heal = None
    app._health_healing = False
    app._self_call_until = {}
    app._ignore_abb_rings_until = None
    app.ring_ack_message = "One moment, please."
    app.no_answer_message = "Sorry, no one can answer the door right now."
    app.reject_message = "Sorry, we cannot open the door right now."
    app.no_answer_after_s = 45
    app.ring_action_window_s = 180
    app.lock_by_door = {"front door": "lock.intercomproxy_front_door",
                        "back door": "lock.intercomproxy_back_door"}
    app._pending_actions = {}
    app._state_file = tmp / "abb_welcome_bridge_state.json"
    app.counters = {"rings_both": 0, "rings_esp_only": 0, "rings_abb_only": 0,
                    "door_opens_both": 0, "door_opens_abb_only": 0}
    app.lag_stats = {"sum_ms": 0.0, "n": 0, "last_ms": None}
    app.processed_missed_ids = []
    app.station_door_matrix = {}

    # duck-typed AD surface
    app.get_now = clock.now
    app.logs = []
    app.log = lambda msg, level="INFO": app.logs.append((level, msg))
    app.states = {}  # (entity, attribute) -> value; entity -> {"attributes": ...} for attribute="all"

    def fake_get_state(entity, attribute=None):
        if attribute == "all":
            return app.states.get(entity)
        return app.states.get((entity, attribute))
    app.get_state = fake_get_state

    app.run_in_calls = []

    def fake_run_in(callback, delay, **kwargs):
        app.run_in_calls.append((callback, delay, kwargs))
        return f"handle-{len(app.run_in_calls)}"
    app.run_in = fake_run_in

    def inline_executor(fn, *args, **kwargs):
        fn(*args, **kwargs)
    app.submit_to_executor = inline_executor

    app.pushes = []

    class FakeNotifier:
        def notify(self, **kwargs):  # plain callable: bridge wraps it in create_task
            app.pushes.append(kwargs)
            return None
    app.mobile_notifier = FakeNotifier()
    app.created_tasks = []
    app.create_task = lambda x: app.created_tasks.append(x)
    app.fired_events = []
    app.fire_event = lambda event, **kwargs: app.fired_events.append((event, kwargs))
    app.published = []
    app.set_state = lambda entity, **kwargs: app.published.append((entity, kwargs))
    app.service_calls = []
    app.call_service = lambda service, **kwargs: app.service_calls.append((service, kwargs))
    return app


def _run_scheduled(app, callback_name):
    """Run (and consume) every captured run_in call bound to the given method.
    Snapshots the pending list first: a callback that re-schedules itself (the
    clip's announce-collision defer) must land in run_in_calls for a LATER pass,
    not execute in this one."""
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


def _missed_attrs(event_id=MISSED_ID, timestamp=MISSED_TS):
    return {"event_type": "call-missed", "event_id": event_id, "timestamp": timestamp,
            "station_id": "", "station": ""}


class PureHelperTests(unittest.TestCase):
    def test_classify_unlock_beats_missed(self):
        # The 14:24 reality: door opened by auto-open, portal still said missed.
        self.assertEqual(bridge_mod.classify_episode(True, True, False), "ring_auto_opened")

    def test_classify_order(self):
        self.assertEqual(bridge_mod.classify_episode(False, False, True), "ring_answered")
        self.assertEqual(bridge_mod.classify_episode(False, True, False), "ring_missed")
        self.assertEqual(bridge_mod.classify_episode(False, False, False), "ring")

    def test_lag_ms_sign_and_value(self):
        lag = bridge_mod.lag_ms(RING_ESP, RING_ABB)
        self.assertAlmostEqual(lag, -53.0, delta=0.5)  # negative = ABB first
        self.assertIsNone(bridge_mod.lag_ms(None, RING_ABB))
        self.assertIsNone(bridge_mod.lag_ms(RING_ESP, None))

    def test_door_slug(self):
        self.assertEqual(bridge_mod.door_slug("front door", ""), "front")
        self.assertEqual(bridge_mod.door_slug("back door", "100000001"), "back")
        self.assertEqual(bridge_mod.door_slug(None, "100000001"), "station_100000001")
        self.assertEqual(bridge_mod.door_slug("", ""), "unknown")

    def test_index_entry_exact_shape(self):
        entry = bridge_mod.build_index_entry(RING_ESP, "100000001", "back door",
                                             "ring_auto_opened", "f.jpg", "/local/abb_doorbell")
        self.assertEqual(set(entry), {"ts", "datetime", "station", "door", "event_type", "filename", "url"})
        self.assertEqual(entry["ts"], int(RING_ESP.timestamp()))
        self.assertEqual(entry["url"], "/local/abb_doorbell/f.jpg")
        self.assertEqual(entry["event_type"], "ring_auto_opened")

    def test_prune_by_days_and_count(self):
        now_ts = 1_000_000_000
        fresh = [{"ts": now_ts - i, "filename": f"f{i}.jpg"} for i in range(5)]
        old = [{"ts": now_ts - 91 * 86400, "filename": "old.jpg"}]
        keep, drop = bridge_mod.prune_images(fresh + old, now_ts, retain_days=90, max_files=100)
        self.assertEqual(len(keep), 5)
        self.assertEqual([d["filename"] for d in drop], ["old.jpg"])
        keep, drop = bridge_mod.prune_images(fresh, now_ts, retain_days=90, max_files=3)
        self.assertEqual(len(keep), 3)
        self.assertEqual(len(drop), 2)

    def test_parse_iso_ts(self):
        self.assertEqual(bridge_mod.parse_iso_ts("2026-08-12T14:24:47+00:00"),
                         datetime(2026, 8, 12, 14, 24, 47, tzinfo=timezone.utc))
        self.assertEqual(bridge_mod.parse_iso_ts("2026-08-12T14:24:47Z"),
                         datetime(2026, 8, 12, 14, 24, 47, tzinfo=timezone.utc))
        self.assertIsNone(bridge_mod.parse_iso_ts(None))
        self.assertIsNone(bridge_mod.parse_iso_ts("garbage"))


class MissedCallTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.clock = Clock(RING_ABB)
        self.app = _bare_bridge(tmpdir.name, self.clock)
        # The 14:24 fixture day ran with auto-open ON (that is what answered the
        # door) - so the auto-open-off ring fallback stays out of these replays,
        # and the missed push keeps its original role as the auto-open safety net.
        self.app.states[("input_boolean.auto_open_intercom", None)] = "on"

    def _ring_and_missed(self):
        """Replay the 14:24 sequence up to the missed event's arrival."""
        self.clock.at = RING_ABB
        self.app._on_abb_bus_ring("abb_welcome_ring", {
            "station_id": "100000001", "received_at": RING_ABB.timestamp()}, {})
        self.clock.at = RING_ESP
        self.app._register_ring("esp", "back door", "", RING_ESP, "esp:test")
        self.clock.at = MISSED_ARRIVED
        self.app._handle_missed(_missed_attrs())

    def test_auto_opened_ring_suppresses_missed_push(self):
        # THE 14:24 fixture: unlock evidence at ring+9.0 s => no "missed" push.
        self._ring_and_missed()
        self.clock.at = UNLOCK_AT
        self.app._on_lock_activity("lock.intercomproxy_back_door", "state", "locked", "unlocking", {})
        self.clock.at = RING_ESP + timedelta(seconds=16)
        self.assertEqual(_run_scheduled(self.app, "_decide_missed"), 1)
        self.assertEqual(self.app.pushes, [])
        self.assertTrue(any("MISSED-SUPPRESSED" in msg for _, msg in self.app.logs))

    def test_genuine_miss_pushes_once_with_snapshot(self):
        self.app.states[("image.abb_screenshot", "entity_picture")] = "/api/image_proxy/image.abb_screenshot?token=abc"
        self._ring_and_missed()  # no unlock recorded
        self.clock.at = RING_ESP + timedelta(seconds=16)
        _run_scheduled(self.app, "_decide_missed")
        self.assertEqual(len(self.app.pushes), 1)
        push = self.app.pushes[0]
        self.assertEqual(push["target"], "home")
        self.assertEqual(push["data"], {"data": {"image": "/api/image_proxy/image.abb_screenshot?token=abc"}})
        self.assertIn("back door", push["message"])
        self.assertEqual(len(self.app.created_tasks), 1)
        # and the house feed heard about it
        self.assertTrue(any(e == "house_events_report" for e, _ in self.app.fired_events))

    def test_missed_event_id_debounced_and_rearmed(self):
        self._ring_and_missed()
        self.app._handle_missed(_missed_attrs())  # same portal event again
        self.assertEqual(_run_scheduled(self.app, "_decide_missed"), 1)  # one decision only
        self.assertEqual(len(self.app.pushes), 1)
        # a DISTINCT later missed event re-arms
        later_ring = RING_ESP + timedelta(minutes=10)
        self.clock.at = later_ring
        self.app._register_ring("esp", "front door", "", later_ring, "esp:test")
        self.clock.at = later_ring + timedelta(seconds=7)
        self.app._handle_missed(_missed_attrs(event_id="other-id",
                                              timestamp=(later_ring + timedelta(seconds=3)).isoformat()))
        self.clock.at = later_ring + timedelta(seconds=16)
        _run_scheduled(self.app, "_decide_missed")
        self.assertEqual(len(self.app.pushes), 2)

    def test_stale_missed_event_ignored(self):
        self.clock.at = RING_ESP + timedelta(hours=2)
        self.app._handle_missed(_missed_attrs())
        self.assertEqual(_run_scheduled(self.app, "_decide_missed"), 0)
        self.assertEqual(self.app.pushes, [])

    def test_missing_notifier_and_image_degrade_silently(self):
        self.app.mobile_notifier = None  # notifier absent
        # entity_picture absent too (no ABB image entity)
        self._ring_and_missed()
        self.clock.at = RING_ESP + timedelta(seconds=16)
        _run_scheduled(self.app, "_decide_missed")  # must not raise
        self.assertEqual(self.app.pushes, [])
        self.assertTrue(any("MISSED-CONFIRMED" in msg for _, msg in self.app.logs))

    def test_missed_without_episode_uses_lock_evidence(self):
        # Bridge restarted mid-ring: no episode, but the unlock still suppresses.
        self.clock.at = UNLOCK_AT
        self.app._on_lock_activity("lock.intercomproxy_back_door", "state", "locked", "unlocking", {})
        self.clock.at = MISSED_ARRIVED
        self.app._handle_missed(_missed_attrs())
        self.clock.at = MISSED_ARRIVED + timedelta(seconds=15)
        _run_scheduled(self.app, "_decide_missed")
        self.assertEqual(self.app.pushes, [])
        self.assertTrue(any("MISSED-SUPPRESSED" in msg for _, msg in self.app.logs))


class UnlockEvidenceTests(unittest.TestCase):
    """Per-door unlock evidence (2026-08-24 fix): a LIST of recent unlock
    timestamps per door, not a single last-writer-wins slot, and last_changed
    preferred over processing time so a blocked pinned thread cannot mis-stamp
    a real unlock outside suppress_window_s."""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.clock = Clock(datetime(2026, 8, 24, 18, 11, 0, tzinfo=timezone.utc))
        self.app = _bare_bridge(tmpdir.name, self.clock)

    def test_five_ring_visitor_still_classifies_auto_opened(self):
        # Real 2026-08-24 18:11 case: ring, then unlocks at +6.1/+20.5/+23.7/
        # +31.8 s. The first three are in-window (suppress_window_s=30); a
        # single-slot store used to keep only the last (+31.8 s, out of
        # window) and archive this as "ring_missed" even though the door
        # opened fine.
        ring_at = self.clock.at
        self.app._register_ring("esp", "front door", "", ring_at, "esp:test")
        for offset in (6.1, 20.5, 23.7, 31.8):
            self.clock.at = ring_at + timedelta(seconds=offset)
            self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                       "locked", "unlocking", {})
        unlock_at = self.app._unlock_near("front door", ring_at)
        self.assertIsNotNone(unlock_at)
        self.assertAlmostEqual((unlock_at - ring_at).total_seconds(), 6.1, places=2)
        event_type = bridge_mod.classify_episode(unlock_at is not None, False, False)
        self.assertEqual(event_type, "ring_auto_opened")

    def test_unlock_near_returns_closest_in_window_and_none_when_all_outside(self):
        ring_at = self.clock.at
        self.app.unlock_events["front door"] = [
            ring_at + timedelta(seconds=-25),
            ring_at + timedelta(seconds=12),
            ring_at + timedelta(seconds=3),
        ]
        # Closest to ring_at among the in-window ones is +3 s - not the first
        # stored entry (-25 s) and not the largest in-window one (+12 s).
        self.assertEqual(self.app._unlock_near("front door", ring_at),
                         ring_at + timedelta(seconds=3))
        self.app.unlock_events["front door"] = [
            ring_at + timedelta(seconds=-45),
            ring_at + timedelta(seconds=40),
        ]
        self.assertIsNone(self.app._unlock_near("front door", ring_at))

    def test_unlock_near_door_filter_ignores_other_doors(self):
        ring_at = self.clock.at
        self.app.unlock_events["back door"] = [ring_at + timedelta(seconds=2)]
        self.assertIsNone(self.app._unlock_near("front door", ring_at))
        self.assertEqual(self.app._unlock_near("back door", ring_at),
                         ring_at + timedelta(seconds=2))
        self.assertEqual(self.app._unlock_near(None, ring_at),
                         ring_at + timedelta(seconds=2))  # falsy door = any lock counts

    def test_last_changed_preferred_when_sane(self):
        changed_at = self.clock.at - timedelta(seconds=5)  # true edge, 5 s before we process it
        self.app.states[("lock.intercomproxy_front_door", "last_changed")] = changed_at.isoformat()
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "locked", "unlocking", {})
        self.assertEqual(self.app.unlock_events["front door"], [changed_at])

    def test_last_changed_missing_falls_back_to_now(self):
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "locked", "unlocking", {})
        self.assertEqual(self.app.unlock_events["front door"], [self.clock.at])

    def test_last_changed_unparseable_falls_back_to_now(self):
        self.app.states[("lock.intercomproxy_front_door", "last_changed")] = "not-a-timestamp"
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "locked", "unlocking", {})
        self.assertEqual(self.app.unlock_events["front door"], [self.clock.at])

    def test_last_changed_in_future_falls_back_to_now(self):
        future = self.clock.at + timedelta(seconds=5)
        self.app.states[("lock.intercomproxy_front_door", "last_changed")] = future.isoformat()
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "locked", "unlocking", {})
        self.assertEqual(self.app.unlock_events["front door"], [self.clock.at])

    def test_last_changed_too_old_falls_back_to_now(self):
        stale = self.clock.at - timedelta(seconds=601)
        self.app.states[("lock.intercomproxy_front_door", "last_changed")] = stale.isoformat()
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "locked", "unlocking", {})
        self.assertEqual(self.app.unlock_events["front door"], [self.clock.at])

    def test_last_changed_lookup_exception_falls_back_to_now(self):
        def boom(entity, attribute=None):
            raise RuntimeError("cache miss")
        self.app.get_state = boom
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "locked", "unlocking", {})
        self.assertEqual(self.app.unlock_events["front door"], [self.clock.at])
        self.assertTrue(any("last_changed lookup failed" in m for _, m in self.app.logs))

    def test_list_pruned_by_age_and_capped_in_length(self):
        door = "front door"
        base = self.clock.at
        # An entry well past UNLOCK_EVENT_TTL_S (600 s) must not survive a new append.
        self.app.unlock_events[door] = [base - timedelta(seconds=700)]
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "locked", "unlocking", {})
        self.assertEqual(self.app.unlock_events[door], [base])
        # Cap: appending onto a list already at/over the cap keeps only the newest.
        self.app.unlock_events[door] = [base + timedelta(seconds=i) for i in range(25)]
        self.clock.at = base + timedelta(seconds=25)
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state",
                                   "unlocking", "unlocked", {})
        self.assertEqual(len(self.app.unlock_events[door]), bridge_mod.UNLOCK_EVENT_MAX_PER_DOOR)
        self.assertEqual(self.app.unlock_events[door][-1], base + timedelta(seconds=25))

    def test_door_open_comparator_pairs_against_any_stored_unlock(self):
        ring_at = self.clock.at
        # Two doors, several entries each; only one entry is close enough to pair.
        self.app.unlock_events["back door"] = [ring_at - timedelta(seconds=100)]
        self.app.unlock_events["front door"] = [
            ring_at - timedelta(seconds=200),
            ring_at + timedelta(seconds=40),  # the one that should pair
        ]
        self.app._on_abb_event("event.abb", "all", None, {"attributes": {
            "event_type": "door-open",
            "timestamp": (ring_at + timedelta(seconds=45)).isoformat(),
        }}, {})
        self.assertEqual(self.app.counters["door_opens_both"], 1)
        self.assertEqual(self.app.counters["door_opens_abb_only"], 0)


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmp = tmpdir.name
        self.clock = Clock(RING_ABB)
        self.app = _bare_bridge(self.tmp, self.clock)

    def test_both_sides_counted_with_lag(self):
        self.app._on_abb_bus_ring("abb_welcome_ring", {
            "station_id": "100000001", "received_at": RING_ABB.timestamp()}, {})
        self.clock.at = RING_ESP
        self.app._register_ring("esp", "back door", "", RING_ESP, "esp:test")
        self.assertEqual(self.app.counters["rings_both"], 1)
        self.assertAlmostEqual(self.app.lag_stats["last_ms"], -53.0, delta=0.5)
        self.assertEqual(self.app.station_door_matrix, {"100000001": {"back door": 1}})
        self.assertTrue(any("RING-CMP MATCH" in m for _, m in self.app.logs))
        # published sensor carries the counters
        entity, kwargs = self.app.published[-1]
        self.assertEqual(entity, "sensor.abb_esp_ring_agreement")
        self.assertEqual(kwargs["attributes"]["rings_both"], 1)
        self.assertEqual(kwargs["state"], "1")

    def test_esp_only_and_abb_only_counted_on_window_expiry(self):
        self.app._register_ring("esp", "front door", "", RING_ESP, "esp:test")
        self.clock.at = RING_ESP + timedelta(seconds=11)
        _run_scheduled(self.app, "_pair_check")
        self.assertEqual(self.app.counters["rings_esp_only"], 1)
        self.assertTrue(any("RING-CMP ESP-ONLY" in m for _, m in self.app.logs))
        at2 = RING_ESP + timedelta(minutes=5)
        self.clock.at = at2
        self.app._on_abb_bus_ring("abb_welcome_ring", {
            "station_id": "100000002", "received_at": at2.timestamp()}, {})
        self.clock.at = at2 + timedelta(seconds=11)
        _run_scheduled(self.app, "_pair_check")
        self.assertEqual(self.app.counters["rings_abb_only"], 1)
        self.assertTrue(any("RING-CMP ABB-ONLY" in m for _, m in self.app.logs))

    def test_mean_lag_over_two_pairs(self):
        self.app._on_abb_bus_ring("abb_welcome_ring", {
            "station_id": "100000001", "received_at": RING_ABB.timestamp()}, {})
        self.app._register_ring("esp", "back door", "", RING_ESP, "esp:test")
        t2_abb = RING_ABB + timedelta(minutes=3)
        t2_esp = t2_abb + timedelta(milliseconds=47)
        self.clock.at = t2_abb
        self.app._on_abb_bus_ring("abb_welcome_ring", {
            "station_id": "100000002", "received_at": t2_abb.timestamp()}, {})
        self.clock.at = t2_esp
        self.app._register_ring("esp", "front door", "", t2_esp, "esp:test")
        self.assertEqual(self.app.counters["rings_both"], 2)
        mean = self.app.lag_stats["sum_ms"] / self.app.lag_stats["n"]
        self.assertAlmostEqual(mean, -50.0, delta=1.0)

    def test_ringing_sensor_backup_folds_into_bus_ring(self):
        # Bus event and ringing sensor fire off the SAME SIP invite - one episode.
        self.app._on_abb_bus_ring("abb_welcome_ring", {
            "station_id": "100000001", "received_at": RING_ABB.timestamp()}, {})
        self.clock.at = RING_ABB + timedelta(milliseconds=30)
        self.app._on_abb_ringing_edge("binary_sensor.abb_ringing", "state", None, {
            "state": "on", "attributes": {"station_id": "100000001"}}, {})
        self.assertEqual(len(self.app.episodes), 1)

    def test_counters_persist_across_restart(self):
        self.app._on_abb_bus_ring("abb_welcome_ring", {
            "station_id": "100000001", "received_at": RING_ABB.timestamp()}, {})
        self.app._register_ring("esp", "back door", "", RING_ESP, "esp:test")
        reborn = _bare_bridge(self.tmp, self.clock)
        persisted = reborn._load_state()
        self.assertEqual(persisted["counters"]["rings_both"], 1)
        self.assertEqual(persisted["station_door_matrix"], {"100000001": {"back door": 1}})


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.clock = Clock(RING_ESP)
        self.app = _bare_bridge(tmpdir.name, self.clock)
        self.app._fetch_image_bytes = lambda: JPEG

    def _image_update(self, captured_at, event_id):
        self.app.states["image.abb_screenshot"] = {
            "state": captured_at.isoformat(),
            "attributes": {"captured_at": captured_at.isoformat(), "event_id": event_id},
        }
        self.app._on_image_update("image.abb_screenshot", "all", None,
                                  self.app.states["image.abb_screenshot"], {})

    def test_one_episode_one_file_with_index(self):
        self.app._register_ring("esp", "back door", "", RING_ESP, "esp:test")
        self.clock.at = UNLOCK_AT
        self.app._on_lock_activity("lock.intercomproxy_back_door", "state", "locked", "unlocking", {})
        # the fresh screenshot lands on the poll ~7 s later (as measured today)
        self.clock.at = RING_ESP + timedelta(seconds=7)
        self._image_update(RING_ESP + timedelta(seconds=1), "shot-1")
        self._image_update(RING_ESP + timedelta(seconds=1), "shot-1")  # state repeat: no re-fetch
        self.clock.at = RING_ESP + timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        jpgs = list(self.app.archive_dir.glob("*.jpg"))
        self.assertEqual(len(jpgs), 1)
        self.assertIn("_back_ring_auto_opened.jpg", jpgs[0].name)
        index = json.loads((self.app.archive_dir / "index.json").read_text())
        self.assertEqual(index["version"], 1)
        self.assertIn("updated", index)
        self.assertEqual(len(index["images"]), 1)
        row = index["images"][0]
        self.assertEqual(set(row), {"ts", "datetime", "station", "door", "event_type", "filename", "url"})
        self.assertEqual(row["door"], "back door")
        self.assertEqual(row["event_type"], "ring_auto_opened")
        self.assertEqual(row["url"], f"/local/abb_doorbell/{jpgs[0].name}")
        self.assertEqual(row["ts"], int(RING_ESP.timestamp()))

    def test_stale_snapshot_never_archived(self):
        # At ring time the image entity still holds the PREVIOUS visitor.
        self.app._register_ring("esp", "front door", "", RING_ESP, "esp:test")
        self._image_update(RING_ESP - timedelta(hours=2), "old-shot")
        self.clock.at = RING_ESP + timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        self.assertEqual(list(self.app.archive_dir.glob("*.jpg")), [])
        self.assertTrue(any("no fresh snapshot" in m for _, m in self.app.logs))

    def test_fetch_failure_drops_silently(self):
        self.app._fetch_image_bytes = lambda: None
        self.app._register_ring("esp", "front door", "", RING_ESP, "esp:test")
        self._image_update(RING_ESP + timedelta(seconds=1), "shot-1")
        self.clock.at = RING_ESP + timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")  # must not raise
        self.assertEqual(list(self.app.archive_dir.glob("*.jpg")), [])

    def test_later_fresh_frame_replaces_earlier(self):
        self.app._register_ring("esp", "front door", "", RING_ESP, "esp:test")
        self._image_update(RING_ESP + timedelta(seconds=1), "shot-1")
        self.app._fetch_image_bytes = lambda: JPEG + b"v2"
        self._image_update(RING_ESP + timedelta(seconds=20), "shot-2")  # re-ring's later frame
        self.clock.at = RING_ESP + timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        jpgs = list(self.app.archive_dir.glob("*.jpg"))
        self.assertEqual(len(jpgs), 1)
        self.assertEqual(jpgs[0].read_bytes(), JPEG + b"v2")

    def test_prune_removes_old_files_and_entries(self):
        self.app.max_files = 3
        base = RING_ESP - timedelta(days=100)
        # a file old enough to fall out of retain_days
        old_entry = bridge_mod.build_index_entry(base, "", "front door", "ring", "old.jpg", "/local/abb_doorbell")
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.app.archive_dir / "old.jpg").write_bytes(JPEG)
        index_file = self.app.archive_dir / "index.json"
        index_file.write_text(json.dumps({"version": 1, "updated": "x", "images": [old_entry]}))
        self.app._archive_write(JPEG, RING_ESP.isoformat(), "100000002", "front door", "ring")
        index = json.loads(index_file.read_text())
        self.assertEqual(len(index["images"]), 1)  # old-by-days entry pruned
        self.assertFalse((self.app.archive_dir / "old.jpg").exists())
        # count-based prune: three more distinct-timestamp writes -> max_files=3 wins
        for i in range(3):
            self.app._archive_write(JPEG, (RING_ESP + timedelta(minutes=i + 1)).isoformat(),
                                    "100000002", "front door", "ring")
        index = json.loads(index_file.read_text())
        self.assertEqual(len(index["images"]), 3)
        self.assertEqual(len(list(self.app.archive_dir.glob("*.jpg"))), 3)

    def test_frame_goes_to_the_ring_that_produced_it(self):
        # Both doors ring within one episode window: a frame captured after the
        # SECOND ring belongs to the second episode - the first must keep its own.
        self.app._register_ring("esp", "front door", "", RING_ESP, "esp:test")
        self._image_update(RING_ESP + timedelta(seconds=2), "front-shot")
        self.clock.at = RING_ESP + timedelta(seconds=40)
        self.app._register_ring("esp", "back door", "", self.clock.at, "esp:test")
        self.app._fetch_image_bytes = lambda: JPEG + b"back"
        self._image_update(RING_ESP + timedelta(seconds=42), "back-shot")
        episodes = {e["door"]: e for e in self.app.episodes.values()}
        self.assertEqual(episodes["front door"]["snapshot"]["event_id"], "front-shot")
        self.assertEqual(episodes["back door"]["snapshot"]["event_id"], "back-shot")

    def test_missed_classification_tags_index(self):
        self.app._register_ring("esp", "back door", "", RING_ESP, "esp:test")
        self.clock.at = MISSED_ARRIVED
        self.app._handle_missed(_missed_attrs())
        self._image_update(RING_ESP + timedelta(seconds=1), "shot-1")
        self.clock.at = RING_ESP + timedelta(seconds=16)
        _run_scheduled(self.app, "_decide_missed")  # genuine miss (no unlock)
        self.clock.at = RING_ESP + timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        index = json.loads((self.app.archive_dir / "index.json").read_text())
        self.assertEqual(index["images"][0]["event_type"], "ring_missed")
        self.assertEqual(len(self.app.pushes), 1)  # and the push went out


class RingEpisodeFoldTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.clock = Clock(RING_ESP)
        self.app = _bare_bridge(tmpdir.name, self.clock)

    def test_button_bounce_stays_one_episode(self):
        self.app._register_ring("esp", "front door", "", RING_ESP, "esp:test")
        for ms in (300, 900, 1500):  # visitor mashing the button
            self.clock.at = RING_ESP + timedelta(milliseconds=ms)
            self.app._register_ring("esp", "front door", "", self.clock.at, "esp:test")
        self.assertEqual(len(self.app.episodes), 1)
        episode = next(iter(self.app.episodes.values()))
        self.assertEqual(episode["re_rings"], 0)  # inside debounce: bounce, not re-ring

    def test_two_doors_two_episodes(self):
        self.app._register_ring("esp", "front door", "", RING_ESP, "esp:test")
        self.clock.at = RING_ESP + timedelta(seconds=2)
        self.app._register_ring("esp", "back door", "", self.clock.at, "esp:test")
        self.assertEqual(len(self.app.episodes), 2)


class IntercomAttachmentSeamTests(unittest.TestCase):
    """The strictly-additive seam in intercom.py: pushes gain an image when the
    bridge offers one, and remain byte-identical to today when it cannot."""

    def _bare_intercom(self):
        app = intercom.Intercom.__new__(intercom.Intercom)
        app.abb_bridge = None
        app.logs = []
        app.log = lambda msg, level="INFO": app.logs.append((level, msg))
        app.notify_target = "mikkel"
        app.unlock_outcomes = {}
        app.unlock_repeat_count = 3
        app.pushes = []

        class FakeNotifier:
            def notify(self, **kwargs):
                app.pushes.append(kwargs)
                return None
        app.mobile_notifier = FakeNotifier()
        app.create_task = lambda x: None
        app.fire_event = lambda *a, **kw: None
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        app._state_file = Path(tmpdir.name) / "intercom_state.json"
        return app

    def test_attachment_passed_through_on_success_push(self):
        app = self._bare_intercom()
        payload = {"data": {"image": "/api/image_proxy/image.x?token=t"}}

        class FakeBridge:
            def ring_attachment_data(self, door=None):
                return payload

            def defer_ring_push(self, door, title, message, target):
                return False  # declines: intercom sends it itself
        app.get_app = lambda name: FakeBridge()
        app._report_auto_open_success("binary_sensor.front", "lock.front",
                                      {"ring_label": "front door"}, 1)
        self.assertEqual(app.pushes[0]["data"], payload)

    def test_bridge_taking_the_push_stops_intercom_sending_it(self):
        # Exactly one notification per ring: when the bridge holds the push to
        # wait for this visitor's photo, intercom must not also send its own.
        app = self._bare_intercom()
        taken = {}

        class FakeBridge:
            def ring_attachment_data(self, door=None):
                raise AssertionError("must not be consulted once the push is deferred")

            def defer_ring_push(self, door, title, message, target):
                taken.update(door=door, title=title, message=message, target=target)
                return True
        app.get_app = lambda name: FakeBridge()
        app._report_auto_open_success("binary_sensor.front", "lock.front",
                                      {"ring_label": "front door"}, 1)
        self.assertEqual(app.pushes, [])
        self.assertEqual(taken["door"], "front door")
        self.assertEqual(taken["target"], "mikkel")
        self.assertIn("front door", taken["message"])

    def test_bridge_declining_the_defer_still_pushes_immediately(self):
        app = self._bare_intercom()

        class FakeBridge:
            def ring_attachment_data(self, door=None):
                return None

            def defer_ring_push(self, door, title, message, target):
                raise RuntimeError("bridge mid-reload")
        app.get_app = lambda name: FakeBridge()
        app._report_auto_open_success("binary_sensor.front", "lock.front",
                                      {"ring_label": "front door"}, 1)
        self.assertEqual(len(app.pushes), 1)

    def test_bridge_absent_push_goes_out_as_today(self):
        app = self._bare_intercom()
        app.get_app = lambda name: None
        app._report_auto_open_success("binary_sensor.front", "lock.front",
                                      {"ring_label": "front door"}, 1)
        self.assertEqual(len(app.pushes), 1)
        self.assertIsNone(app.pushes[0]["data"])

    def test_bridge_raising_push_goes_out_as_today(self):
        app = self._bare_intercom()

        def boom(name):
            raise RuntimeError("bridge exploded")
        app.get_app = boom
        app._report_auto_open_failure("binary_sensor.front", "lock.front",
                                      {"ring_label": "front door"})
        self.assertEqual(len(app.pushes), 1)
        self.assertIsNone(app.pushes[0]["data"])

    def test_bridge_method_raising_returns_none(self):
        app = self._bare_intercom()

        class BadBridge:
            def ring_attachment_data(self, door=None):
                raise ValueError("no image for you")
        app.get_app = lambda name: BadBridge()
        self.assertIsNone(app._abb_ring_attachment("front door"))

    def test_bridge_ring_attachment_data_never_raises(self):
        # the bridge side of the same contract
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        bridge = _bare_bridge(tmpdir.name, Clock(RING_ESP))

        def broken_get_state(entity, attribute=None):
            raise RuntimeError("HA is down")
        bridge.get_state = broken_get_state
        self.assertIsNone(bridge.ring_attachment_data())


class RingPushPhotoTests(unittest.TestCase):
    """The push must show THIS visitor (2026-08-27, Mikkel: "the image that is
    included in the notification is at least a image delayed - the current
    notification have the ring before that").

    Measured that evening: ring 18:57:30.8, auto-open push 18:57:34.6, and the
    gateway's screenshot for that ring only surfaced at 18:57:55.4 - 20.7 s later.
    Every auto-open push carried the previous visitor. The archive has always
    refused a stale frame ("a missing photo is honest, the wrong person is
    misleading"); the push now agrees with it, and waits a few seconds rather
    than settling for nothing."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 27, 18, 57, 30, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)
        self.pushes = []

        class FakeNotifier:
            def notify(inner, **kwargs):
                self.pushes.append(kwargs)
        self.app.mobile_notifier = FakeNotifier()
        self.app.create_task = lambda coro: None

    def _set_screenshot(self, captured_at):
        key = (self.app.abb_image_entity, "captured_at")
        self.app.states[key] = captured_at.isoformat() if captured_at else None
        self.app.states[(self.app.abb_image_entity, "entity_picture")] = \
            "/api/image_proxy/image.abb?token=t"

    # --- freshness gate on the immediate (undeferred) attachment ---

    def test_screenshot_older_than_the_ring_is_not_attached(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        self._set_screenshot(self.clock.now() - timedelta(seconds=40))  # last visitor
        self.assertIsNone(self.app.ring_attachment_data("front door"))

    def test_screenshot_from_this_ring_is_attached(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        self._set_screenshot(self.clock.now() + timedelta(seconds=2))
        data = self.app.ring_attachment_data("front door")
        self.assertEqual(data["data"]["image"], "/api/image_proxy/image.abb?token=t")

    def test_no_open_ring_means_no_photo(self):
        self._set_screenshot(self.clock.now())
        self.assertIsNone(self.app.ring_attachment_data("front door"))

    def test_omitting_the_door_keeps_the_old_unchecked_behaviour(self):
        self._set_screenshot(self.clock.now() - timedelta(seconds=40))
        self.assertIsNotNone(self.app.ring_attachment_data())

    # --- deferred push ---

    def test_push_is_held_until_the_photo_arrives(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self.assertTrue(self.app.defer_ring_push(
            "front door", "Intercom auto-opened", "Someone rang.", "mikkel"))
        self.assertEqual(self.pushes, [])  # nothing yet - no photo of this visitor
        self._set_screenshot(self.clock.now() + timedelta(seconds=6))
        episode["snapshot"] = {"bytes": b"jpeg", "captured_at": self.clock.now(),
                               "event_id": "e1"}
        _run_scheduled(self.app, "_ring_push_due")
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0]["data"]["image"], "/api/image_proxy/image.abb?token=t")

    def test_push_goes_out_text_only_when_no_photo_ever_lands(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.app.defer_ring_push("front door", "Intercom auto-opened", "Someone rang.", "mikkel")
        _run_scheduled(self.app, "_ring_push_due")  # the deadline fires
        self.assertEqual(len(self.pushes), 1)
        self.assertNotIn("image", self.pushes[0]["data"])

    def test_exactly_one_push_even_if_photo_and_deadline_both_fire(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self.app.defer_ring_push("front door", "Intercom auto-opened", "Someone rang.", "mikkel")
        episode["snapshot"] = {"bytes": b"jpeg", "captured_at": self.clock.now(),
                               "event_id": "e1"}
        self._set_screenshot(self.clock.now())
        for _ in range(3):
            _run_scheduled(self.app, "_ring_push_due")
        self.app._close_episode({"episode_id": episode["id"]})
        self.assertEqual(len(self.pushes), 1)

    def test_photo_already_present_sends_immediately(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        episode["snapshot"] = {"bytes": b"jpeg", "captured_at": self.clock.now(),
                               "event_id": "e1"}
        self._set_screenshot(self.clock.now())
        self.app.defer_ring_push("front door", "Intercom auto-opened", "Someone rang.", "mikkel")
        self.assertEqual(len(self.pushes), 1)

    def test_no_open_episode_declines_so_intercom_pushes_itself(self):
        self.assertFalse(self.app.defer_ring_push(
            "front door", "Intercom auto-opened", "Someone rang.", "mikkel"))

    def test_no_notifier_declines(self):
        self.app.mobile_notifier = None
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.assertFalse(self.app.defer_ring_push(
            "front door", "Intercom auto-opened", "Someone rang.", "mikkel"))

    def test_close_flushes_a_push_whose_timer_was_lost(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self.app.defer_ring_push("front door", "Intercom auto-opened", "Someone rang.", "mikkel")
        self.app.run_in_calls = []  # the deadline timer never fires
        self.app._close_episode({"episode_id": episode["id"]})
        self.assertEqual(len(self.pushes), 1)


if __name__ == "__main__":
    unittest.main()


class AnnounceAfterUnlock(unittest.TestCase):
    """Restored 2026-08-13 (the 2026-08-12 box-direct deploy of this policy was wiped by a
    repo sync): when the house answers a ring by opening the door, the door tells the
    visitor. Front only, physical-unlock-gated, ring-gated, one sentence per visitor."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 13, 7, 40, 0))
        self.tmp = tempfile.TemporaryDirectory()
        self.app = _bare_bridge(self.tmp.name, self.clock)

    def _advance(self, seconds):
        self.clock.at = self.clock.at + timedelta(seconds=seconds)

    def tearDown(self):
        self.tmp.cleanup()

    def _ring(self, door="front door"):
        self.app._open_episode(door, "100000002" if door == "front door" else "100000001", self.clock.now())

    def test_front_unlock_after_ring_announces_once(self):
        self._ring()
        self._advance(9)
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state", "locked", "unlocking", {})
        self._advance(2)
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state", "unlocking", "unlocked", {})
        announces = [c for c in self.app.service_calls if c[0] == "abb_welcome/announce"]
        self.assertEqual(len(announces), 1)
        self.assertEqual(announces[0][1]["entity_id"], "camera.abb_front")
        self.assertEqual(announces[0][1]["message"], "The door is open.")

    def test_unlock_without_ring_stays_silent(self):
        """A dashboard unlock with nobody outside must not talk to the street."""
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state", "locked", "unlocking", {})
        self.assertEqual([c for c in self.app.service_calls if c[0] == "abb_welcome/announce"], [])

    def test_back_door_not_configured_stays_silent(self):
        self._ring("back door")
        self._advance(5)
        self.app._on_lock_activity("lock.intercomproxy_back_door", "state", "locked", "unlocking", {})
        self.assertEqual([c for c in self.app.service_calls if c[0] == "abb_welcome/announce"], [])

    def test_ring_window_expiry_stays_silent(self):
        self._ring()
        self._advance(120)
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state", "locked", "unlocking", {})
        self.assertEqual([c for c in self.app.service_calls if c[0] == "abb_welcome/announce"], [])

    def test_announce_failure_never_raises(self):
        self._ring()
        self._advance(5)
        def boom(service, **kwargs):
            raise RuntimeError("SIP busy")
        self.app.call_service = boom
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state", "locked", "unlocking", {})
        self.assertTrue(any("Announce failed" in m for _, m in self.app.logs))


class RingClipTests(unittest.TestCase):
    """Ring video clips (2026-08-13, "thumbnail and opening that give the video"):
    arm + camera.record at ring+clip_delay_s, never overlapping the announce
    (probed live: they refuse each other station-side), clip attached to the
    index entry at episode close only if the mp4 actually landed."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)

    def _clip_calls(self):
        return [c for c in self.app.service_calls
                if c[0] in ("abb_welcome/arm_streaming", "camera/record")]

    def test_ring_schedules_clip_start(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        scheduled = [(cb.__name__, delay) for cb, delay, _ in self.app.run_in_calls]
        self.assertIn(("_start_clip", 0), scheduled)

    def test_no_clip_cameras_schedules_nothing(self):
        self.app.clip_cameras = {}
        self.app._open_episode("front door", "100000002", self.clock.now())
        scheduled = [cb.__name__ for cb, _, _ in self.app.run_in_calls]
        self.assertNotIn("_start_clip", scheduled)

    def test_start_clip_arms_station_then_records(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self.clock.at += timedelta(seconds=8)
        _run_scheduled(self.app, "_start_clip")
        calls = self._clip_calls()
        self.assertEqual(calls[0][0], "abb_welcome/arm_streaming")
        self.assertEqual(calls[0][1]["station_id"], "100000002")
        self.assertEqual(calls[0][1]["duration"], 35)  # clip_seconds + 25 arm margin
        self.assertEqual(calls[1][0], "camera/record")
        self.assertEqual(calls[1][1]["entity_id"], "camera.abb_front")
        self.assertEqual(calls[1][1]["duration"], 10)
        self.assertTrue(calls[1][1]["filename"].startswith("/config/www/abb_doorbell/abb_clip_"))
        self.assertTrue(calls[1][1]["filename"].endswith("_front.mp4"))
        self.assertEqual(episode["clip_filename"], calls[1][1]["filename"].rsplit("/", 1)[1])

    def test_no_announce_door_records_without_deferral(self):
        # The back door has no announce voice, so _last_announce_at never holds it
        # and the dial starts at clip_delay_s flat - the whole point of enabling
        # back-door clips (user 2026-08-19).
        self.app.clip_cameras = {"back door": "camera.abb_back"}
        self.app._open_episode("back door", "100000001", self.clock.now())
        self.clock.at += timedelta(seconds=3)
        _run_scheduled(self.app, "_start_clip")
        calls = self._clip_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["entity_id"], "camera.abb_back")
        self.assertTrue(calls[1][1]["filename"].endswith("_back.mp4"))

    def test_unmapped_door_records_nothing(self):
        self.app._open_episode("back door", "100000001", self.clock.now())
        self.clock.at += timedelta(seconds=8)
        _run_scheduled(self.app, "_start_clip")
        self.assertEqual(self._clip_calls(), [])

    def test_voices_go_through_the_recording_call(self):
        # Video and voice together (user 2026-08-20): with a recording in flight,
        # announce and door voices are injected into the recording's own call via
        # play_audio - never a second dial, which would kill the clip's video.
        self.app.voice_tts_entity = "tts.piper"
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        _run_scheduled(self.app, "_start_clip")
        self.assertIsNotNone(episode["clip_started_at"])
        n_calls = len(self.app.service_calls)
        self.app._maybe_announce("front door")
        self.app._door_voice("front door", "One moment, please.")
        new_calls = self.app.service_calls[n_calls:]
        self.assertEqual([c[0] for c in new_calls],
                         ["abb_welcome/play_audio", "abb_welcome/play_audio"])
        media = new_calls[0][1]["media"]["media_content_id"]
        self.assertIn("media-source://tts/tts.piper", media)
        self.assertIn("The%20door%20is%20open.", media)
        # and crucially: no announce (temporary-call) service went out
        self.assertNotIn("abb_welcome/announce", [c[0] for c in new_calls])

    def test_disabled_tts_keeps_voices_silent_while_recording(self):
        self.app.voice_tts_entity = ""
        self.app._open_episode("front door", "100000002", self.clock.now())
        _run_scheduled(self.app, "_start_clip")
        n_calls = len(self.app.service_calls)
        self.app._maybe_announce("front door")
        self.app._door_voice("front door", "One moment, please.")
        self.assertEqual(len(self.app.service_calls), n_calls)

    def test_voice_allowed_after_recording_window(self):
        # clip_seconds 10 + 5 margin: 16 s after the dial the slot is free again,
        # so the no-answer message (fires at +45 s) always gets through.
        self.app._open_episode("front door", "100000002", self.clock.now())
        _run_scheduled(self.app, "_start_clip")
        self.clock.at += timedelta(seconds=16)
        n_calls = len(self.app.service_calls)
        self.app._door_voice("front door", "Sorry, no one can answer.")
        self.assertEqual(len(self.app.service_calls), n_calls + 1)

    def test_refused_dial_retries_once_after_confirm(self):
        # The station's own ring call can refuse the first dial SILENTLY - the
        # camera never enters "recording". The confirm check clears the claim and
        # redials once; a confirmed recording is left alone.
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        _run_scheduled(self.app, "_start_clip")
        first = len(self._clip_calls())
        self.assertEqual(first, 2)  # arm + record went out
        # the fake get_state keys plain-state reads on (entity, None)
        self.app.states[("camera.abb_front", None)] = "idle"  # refusal: stream never opened
        self.clock.at += timedelta(seconds=3)
        _run_scheduled(self.app, "_confirm_clip_recording")
        self.assertIsNone(episode["clip_filename"])   # claim cleared
        _run_scheduled(self.app, "_start_clip")       # the redial
        self.assertEqual(len(self._clip_calls()), 4)
        self.assertIsNotNone(episode["clip_filename"])
        # confirmed recording: another confirm invocation must change nothing
        self.app.states[("camera.abb_front", None)] = "recording"
        self.app._confirm_clip_recording({"episode_id": episode["id"]})
        self.assertIsNotNone(episode["clip_filename"])
        self.assertEqual(len(self._clip_calls()), 4)
        # and a refusal AFTER the one retry stays retried=False-free: redial was
        # issued with retried=True, which schedules no further confirm
        confirm_pending = [cb for cb, _, _ in self.app.run_in_calls
                           if cb.__name__ == "_confirm_clip_recording"]
        self.assertEqual(confirm_pending, [])

    def test_clip_service_failure_is_logged_never_raised(self):
        # The dial itself is offloaded (submit_to_executor - see _start_clip), so a
        # call_service failure surfaces from _dial_clip's own try/except, not
        # _start_clip's (which has already returned by the time a REAL executor
        # thread would hit this - the test fixture's inline executor just makes it
        # observable synchronously).
        self.app._open_episode("front door", "100000002", self.clock.now())
        def boom(service, **kwargs):
            raise RuntimeError("stream in use")
        self.app.call_service = boom
        self.clock.at += timedelta(seconds=8)
        _run_scheduled(self.app, "_start_clip")
        self.assertTrue(any("Clip dial failed" in m for _, m in self.app.logs))

    def test_archive_entry_gains_clip_only_when_file_landed(self):
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.app.archive_dir / "abb_clip_x_front.mp4").write_bytes(b"mp4data")
        self.app._archive_write(JPEG, RING_ESP.isoformat(), "100000002", "front door",
                                "ring_auto_opened", "abb_clip_x_front.mp4")
        entry = self.app._load_index()[0]
        self.assertEqual(entry["clip_filename"], "abb_clip_x_front.mp4")
        self.assertEqual(entry["clip_url"], "/local/abb_doorbell/abb_clip_x_front.mp4")
        # Missing file: photo-only entry, no clip keys at all.
        self.app._archive_write(JPEG, (RING_ESP + timedelta(minutes=1)).isoformat(),
                                "100000002", "front door", "ring", "abb_clip_gone.mp4")
        entry = self.app._load_index()[0]
        self.assertNotIn("clip_url", entry)
        self.assertNotIn("clip_filename", entry)

    def test_empty_clip_turd_is_removed(self):
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        turd = self.app.archive_dir / "abb_clip_dead_front.mp4"
        turd.write_bytes(b"")
        self.app._archive_write(JPEG, RING_ESP.isoformat(), "100000002", "front door",
                                "ring", "abb_clip_dead_front.mp4")
        self.assertFalse(turd.exists())
        self.assertNotIn("clip_url", self.app._load_index()[0])

    def test_prune_removes_clip_with_its_photo(self):
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        old_ring = RING_ESP - timedelta(days=100)
        old_entry = bridge_mod.build_index_entry(old_ring, "", "front door", "ring",
                                                 "old.jpg", "/local/abb_doorbell")
        old_entry["clip_filename"] = "old_clip.mp4"
        old_entry["clip_url"] = "/local/abb_doorbell/old_clip.mp4"
        (self.app.archive_dir / "old.jpg").write_bytes(JPEG)
        (self.app.archive_dir / "old_clip.mp4").write_bytes(b"mp4data")
        index_file = self.app.archive_dir / "index.json"
        index_file.write_text(json.dumps({"version": 1, "updated": "x", "images": [old_entry]}))
        self.app._archive_write(JPEG, RING_ESP.isoformat(), "100000002", "front door", "ring")
        self.assertFalse((self.app.archive_dir / "old.jpg").exists())
        self.assertFalse((self.app.archive_dir / "old_clip.mp4").exists())

    def test_clip_survives_a_ring_with_no_snapshot(self):
        # The 2026-08-13 13:06 front ring: recording landed, gateway screenshot
        # never arrived. The old close path archived NOTHING - clip orphaned on
        # disk, ring invisible in the gallery. Now: a clip-only index entry.
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        episode["clip_filename"] = "abb_clip_t_front.mp4"
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.app.archive_dir / "abb_clip_t_front.mp4").write_bytes(b"mp4data")
        self.clock.at += timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        entry = self.app._load_index()[0]
        self.assertEqual(entry["filename"], "")
        self.assertEqual(entry["url"], "")
        self.assertEqual(entry["clip_url"], "/local/abb_doorbell/abb_clip_t_front.mp4")
        self.assertEqual(entry["door"], "front door")
        self.assertEqual(list(self.app.archive_dir.glob("*.jpg")), [])

    def test_no_snapshot_and_no_clip_file_archives_nothing(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        episode["clip_filename"] = "abb_clip_gone_front.mp4"  # record failed: file never appeared
        self.clock.at += timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        self.assertFalse((self.app.archive_dir / "index.json").exists())
        self.assertTrue(any("Neither photo nor clip" in m for _, m in self.app.logs))

    def test_two_clip_only_entries_coexist(self):
        # Dedup keys on filename-or-clip: with bare filenames ("" for clip-only),
        # every new clip-only entry would swallow all previous ones.
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        (self.app.archive_dir / "abb_clip_a_front.mp4").write_bytes(b"a")
        (self.app.archive_dir / "abb_clip_b_front.mp4").write_bytes(b"b")
        self.app._archive_write(None, RING_ESP.isoformat(), "100000002", "front door",
                                "ring_auto_opened", "abb_clip_a_front.mp4")
        self.app._archive_write(None, (RING_ESP + timedelta(minutes=1)).isoformat(),
                                "100000002", "front door", "ring_auto_opened", "abb_clip_b_front.mp4")
        clips = [e.get("clip_filename") for e in self.app._load_index()]
        self.assertEqual(clips, ["abb_clip_b_front.mp4", "abb_clip_a_front.mp4"])

    def test_orphan_sweep_removes_never_indexed_files(self):
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        day_plus = RING_ESP.timestamp() - 2 * 86400
        old_orphan = self.app.archive_dir / "abb_clip_orphan_front.mp4"
        old_orphan.write_bytes(b"mp4data")
        os.utime(old_orphan, (day_plus, day_plus))
        fresh_orphan = self.app.archive_dir / "abb_clip_inflight_front.mp4"
        fresh_orphan.write_bytes(b"recording")  # mtime now: could be an in-flight record
        foreign = self.app.archive_dir / "concurrency_test.mp4"
        foreign.write_bytes(b"not ours")
        os.utime(foreign, (day_plus, day_plus))
        referenced_clip = self.app.archive_dir / "abb_clip_kept_front.mp4"
        referenced_clip.write_bytes(b"mp4data")
        os.utime(referenced_clip, (day_plus, day_plus))
        self.app._archive_write(JPEG, RING_ESP.isoformat(), "100000002", "front door",
                                "ring_auto_opened", "abb_clip_kept_front.mp4")
        self.assertFalse(old_orphan.exists())          # old + never indexed: swept
        self.assertTrue(fresh_orphan.exists())         # young: grace for in-flight recordings
        self.assertTrue(foreign.exists())              # not abb_*: never ours to delete
        self.assertTrue(referenced_clip.exists())      # indexed: kept


class HealthWatchdogTests(unittest.TestCase):
    """Self-healing ABB health watchdog (2026-08-13, built after the feature audit
    caught the front camera unavailable for 90 minutes: stream workers leaked by
    failed camera.record attempts, cured by one reload_config_entry)."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 13, 11, 0, 0, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)
        self.app.states[("sensor.abb_sip", None)] = "registered"
        self.app.states[("camera.abb_front", None)] = "idle"

    def _reloads(self):
        return [c for c in self.app.service_calls if c[0] == "homeassistant/reload_config_entry"]

    def _tick(self, advance_s=0):
        self.clock.at += timedelta(seconds=advance_s)
        self.app._health_tick({})

    def test_healthy_does_nothing(self):
        self._tick()
        self._tick(60)
        self.assertEqual(self._reloads(), [])
        self.assertEqual(self.app.pushes, [])
        self.assertIsNone(self.app._health_bad_since)

    def test_sustained_sip_failure_reloads_and_pages_mikkel(self):
        self.app.states[("sensor.abb_sip", None)] = "error"
        self._tick()                      # degraded noticed, no action yet
        self.assertEqual(self._reloads(), [])
        self._tick(300)                   # still inside the grace window
        self.assertEqual(self._reloads(), [])
        self._tick(301)                   # 601 s bad -> heal
        self.assertEqual(len(self._reloads()), 1)
        self.assertEqual(self._reloads()[0][1]["entity_id"], "sensor.abb_sip")
        self.assertEqual(len(self.app.pushes), 1)
        self.assertEqual(self.app.pushes[0]["target"], ["mikkel"])
        self.assertIn("SIP listener 'error'", self.app.pushes[0]["message"])

    def test_camera_unavailable_alone_triggers(self):
        self.app.states[("camera.abb_front", None)] = "unavailable"
        self._tick()
        self._tick(601)
        self.assertEqual(len(self._reloads()), 1)
        self.assertIn("camera.abb_front unavailable", self.app.pushes[0]["message"])

    def test_heal_cooldown_limits_reloads_and_pages(self):
        self.app.states[("sensor.abb_sip", None)] = "error"
        self._tick()
        self._tick(601)
        self.assertEqual(len(self._reloads()), 1)
        self._tick(60)                    # still down right after the heal
        self._tick(600)
        self.assertEqual(len(self._reloads()), 1)   # cooldown holds
        self.assertEqual(len(self.app.pushes), 1)
        self._tick(3600)                  # cooldown lapsed, still down -> heal again
        self.assertEqual(len(self._reloads()), 2)
        self.assertEqual(len(self.app.pushes), 2)

    def test_recovery_after_heal_pushes_and_resets(self):
        self.app.states[("sensor.abb_sip", None)] = "error"
        self._tick()
        self._tick(601)
        self.app.states[("sensor.abb_sip", None)] = "registered"
        self._tick(60)
        self.assertEqual(len(self.app.pushes), 2)
        self.assertEqual(self.app.pushes[1]["title"], "ABB intercom recovered")
        self.assertIsNone(self.app._health_bad_since)
        self.assertFalse(self.app._health_healing)

    def test_blip_that_recovers_stays_silent(self):
        self.app.states[("sensor.abb_sip", None)] = "error"
        self._tick()
        self.app.states[("sensor.abb_sip", None)] = "registered"
        self._tick(120)
        self.assertEqual(self._reloads(), [])
        self.assertEqual(self.app.pushes, [])

    def test_unreadable_sensor_is_not_evidence(self):
        del self.app.states[("sensor.abb_sip", None)]     # get_state -> None
        self.app.states[("camera.abb_front", None)] = "idle"
        self._tick()
        self._tick(601)
        self.assertEqual(self._reloads(), [])
        self.assertIsNone(self.app._health_bad_since)


class SelfEvidenceGuardTests(unittest.TestCase):
    """Our own outbound SIP activity must never count as visitor evidence
    (2026-08-13: the watchdog's reload bounced the ringing sensor into a phantom
    unattributed ring at 08:46:01 and polluted rings_abb_only; the portal logs
    our announce/record dials as call-answered)."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)

    def test_reload_grace_drops_only_unattributed_abb_rings(self):
        self.app._ignore_abb_rings_until = self.clock.at + timedelta(seconds=90)
        self.app._register_ring("abb", None, "", self.clock.at, "abb:sensor")
        self.assertEqual(self.app.episodes, {})           # phantom dropped
        self.app._register_ring("abb", "front door", "100000002", self.clock.at, "abb:bus")
        self.assertEqual(len(self.app.episodes), 1)       # a real, attributed ring counts
        # ESP rings are untouched by the grace window.
        self.clock.at += timedelta(seconds=200)
        self.app.episodes.clear()
        self.app._ignore_abb_rings_until = self.clock.at + timedelta(seconds=90)
        self.app._register_ring("esp", "front door", "", self.clock.at, "esp:test")
        self.assertEqual(len(self.app.episodes), 1)

    def test_grace_expires(self):
        self.app._ignore_abb_rings_until = self.clock.at - timedelta(seconds=1)
        self.app._register_ring("abb", None, "", self.clock.at, "abb:sensor")
        self.assertEqual(len(self.app.episodes), 1)

    def test_answered_inside_self_call_window_is_ignored(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.at)
        self.app._self_call_until["front door"] = self.clock.at + timedelta(seconds=30)
        answered_at = self.clock.at + timedelta(seconds=10)
        self.app._on_abb_event("event.abb", "all", None, {"attributes": {
            "event_type": bridge_mod.EVENT_ANSWERED, "timestamp": answered_at.isoformat(),
        }}, {})
        self.assertFalse(episode["answered"])
        # Outside the window the same event counts.
        late_at = self.clock.at + timedelta(seconds=45)
        self.app._on_abb_event("event.abb", "all", None, {"attributes": {
            "event_type": bridge_mod.EVENT_ANSWERED, "timestamp": late_at.isoformat(),
        }}, {})
        self.assertTrue(episode["answered"])

    def test_clip_start_opens_self_call_window(self):
        self.app._open_episode("front door", "100000002", self.clock.at)
        self.clock.at += timedelta(seconds=8)
        _run_scheduled(self.app, "_start_clip")
        until = self.app._self_call_until.get("front door")
        self.assertIsNotNone(until)
        self.assertEqual((until - self.clock.at).total_seconds(), 30)  # clip 10 s + 20 s margin

    def test_announce_opens_self_call_window(self):
        self.app._open_episode("front door", "100000002", self.clock.at)
        self.clock.at += timedelta(seconds=5)
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state", "locked", "unlocking", {})
        self.assertIn("front door", self.app._self_call_until)

    def test_watchdog_heal_opens_ring_grace(self):
        self.app.states[("sensor.abb_sip", None)] = "error"
        self.app.states[("camera.abb_front", None)] = "idle"
        self.app._health_tick({})
        self.clock.at += timedelta(seconds=601)
        self.app._health_tick({})
        self.assertIsNotNone(self.app._ignore_abb_rings_until)
        self.assertEqual((self.app._ignore_abb_rings_until - self.clock.at).total_seconds(), 90)


class RingFallbackTests(unittest.TestCase):
    """Auto-open-OFF fallback (2026-08-13): Open/Reject push, ack voice, no-answer
    voice, reject voice, and the guarantee that any unlock kills stale buttons."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 13, 14, 0, 0, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)
        self.app.states[("input_boolean.auto_open_intercom", None)] = "off"

    def _ring(self, door="front door"):
        self.app._register_ring("esp", door, "", self.clock.at, "esp:test")
        return next(iter(self.app.episodes.values()))

    def _announces(self):
        return [c[1]["message"] for c in self.app.service_calls if c[0] == "abb_welcome/announce"]

    def _entry(self, episode):
        return self.app._pending_actions[f"ABB_RING_OPEN_{episode['id']}"]

    def _press(self, action_id):
        self.app._on_notification_action("mobile_app_notification_action", {"action": action_id}, {})

    def test_ring_with_auto_open_off_pushes_open_reject(self):
        episode = self._ring()
        self.assertTrue(episode["action_push_sent"])
        self.assertEqual(len(self.app.pushes), 1)
        push = self.app.pushes[0]
        self.assertEqual(push["target"], "home")
        actions = push["data"]["actions"]
        self.assertEqual([a["title"] for a in actions], ["Open", "Reject"])
        self.assertEqual(push["data"]["tag"], "abb_ring_front")
        scheduled = [cb.__name__ for cb, _, _ in self.app.run_in_calls]
        self.assertIn("_ring_ack", scheduled)
        self.assertIn("_ring_no_answer", scheduled)

    def test_ring_with_auto_open_on_stays_quiet(self):
        self.app.states[("input_boolean.auto_open_intercom", None)] = "on"
        episode = self._ring()
        self.assertFalse(episode["action_push_sent"])
        self.assertEqual(self.app.pushes, [])

    def test_re_ring_folds_without_second_push(self):
        self._ring()
        self.clock.at += timedelta(seconds=3)
        self.app._register_ring("esp", "front door", "", self.clock.at, "esp:test")
        self.assertEqual(len(self.app.episodes), 1)
        self.assertEqual(len(self.app.pushes), 1)

    def test_ack_speaks_unless_already_buzzed(self):
        self._ring()
        self.clock.at += timedelta(seconds=2)
        _run_scheduled(self.app, "_ring_ack")
        self.assertEqual(self._announces(), ["One moment, please."])
        # Second ring at another time, but the door was already buzzed open.
        self.app.episodes.clear()
        self.clock.at += timedelta(seconds=300)
        self._ring()
        self.app.unlock_events["front door"] = [self.clock.at + timedelta(seconds=1)]
        self.clock.at += timedelta(seconds=2)
        _run_scheduled(self.app, "_ring_ack")
        self.assertEqual(len(self._announces()), 1)  # no second ack

    def test_open_action_unlocks_via_esp_lock_and_clears(self):
        episode = self._ring()
        entry = self._entry(episode)
        self.clock.at += timedelta(seconds=20)
        self._press(entry["open_id"])
        unlocks = [c for c in self.app.service_calls if c[0] == "lock/unlock"]
        self.assertEqual(unlocks, [("lock/unlock", {"entity_id": "lock.intercomproxy_front_door"})])
        self.assertEqual(self.app.pushes[-1]["message"], "clear_notification")
        # The sibling Reject button died with it.
        self._press(entry["reject_id"])
        self.assertNotIn("Sorry, we cannot open the door right now.", self._announces())

    def test_reject_action_speaks_and_silences_everything_after(self):
        episode = self._ring()
        entry = self._entry(episode)
        self.clock.at += timedelta(seconds=15)
        self._press(entry["reject_id"])
        self.assertTrue(episode["rejected"])
        self.assertIn("Sorry, we cannot open the door right now.", self._announces())
        self.assertEqual(self.app.pushes[-1]["message"], "clear_notification")
        # The no-answer timer fires later and must stay silent.
        self.clock.at += timedelta(seconds=30)
        _run_scheduled(self.app, "_ring_no_answer")
        self.assertNotIn("Sorry, no one can answer the door right now.", self._announces())

    def test_expired_action_is_ignored(self):
        episode = self._ring()
        entry = self._entry(episode)
        self.clock.at += timedelta(seconds=181)
        self._press(entry["open_id"])
        self.assertEqual([c for c in self.app.service_calls if c[0] == "lock/unlock"], [])

    def test_no_answer_speaks_and_withdraws_push(self):
        self._ring()
        self.clock.at += timedelta(seconds=45)
        _run_scheduled(self.app, "_ring_no_answer")
        self.assertIn("Sorry, no one can answer the door right now.", self._announces())
        self.assertEqual(self.app.pushes[-1]["message"], "clear_notification")

    def test_unlock_by_any_path_kills_pending_buttons(self):
        episode = self._ring()
        entry = self._entry(episode)
        self.clock.at += timedelta(seconds=10)
        self.app._on_lock_activity("lock.intercomproxy_front_door", "state", "locked", "unlocking", {})
        self.assertEqual(self.app._pending_actions, {})
        self.assertEqual(self.app.pushes[-1]["message"], "clear_notification")
        self._press(entry["open_id"])
        self.assertEqual([c for c in self.app.service_calls if c[0] == "lock/unlock"], [])

    def test_missed_push_suppressed_when_ring_push_handled_it(self):
        episode = self._ring()
        self.clock.at += timedelta(seconds=20)
        self.app._decide_missed({"episode_id": episode["id"], "event_id": "x",
                                 "anchor_iso": self.clock.at.isoformat()})
        missed = [p for p in self.app.pushes if "nobody answered" in str(p.get("message", ""))]
        self.assertEqual(missed, [])
        self.assertTrue(any("MISSED-SUPPRESSED" in m for _, m in self.app.logs))

    def test_door_open_comparator_counts(self):
        # Paired: the ESP unlocked 5 s before the portal reports door-open.
        self.app.unlock_events["front door"] = [self.clock.at]
        self.app._on_abb_event("event.abb", "all", None, {"attributes": {
            "event_type": "door-open",
            "timestamp": (self.clock.at + timedelta(seconds=5)).isoformat(),
        }}, {})
        self.assertEqual(self.app.counters["door_opens_both"], 1)
        # Unpaired: a portal door-open with no ESP unlock anywhere near.
        self.app._on_abb_event("event.abb", "all", None, {"attributes": {
            "event_type": "door-open",
            "timestamp": (self.clock.at + timedelta(seconds=500)).isoformat(),
        }}, {})
        self.assertEqual(self.app.counters["door_opens_abb_only"], 1)
        published = dict(self.app.published)["sensor.abb_esp_ring_agreement"]
        self.assertEqual(published["attributes"]["door_opens_both"], 1)
        self.assertEqual(published["attributes"]["door_opens_abb_only"], 1)


class NativeRingClipTests(unittest.TestCase):
    """native_ring_clips=true (2026-08-24): the integration's own recorder owns
    the ring clip end-to-end - this bridge only listens for its bus event and
    retries a voice into whichever call is open, in place of dialing anything
    itself. With the knob off, every one of these paths must be a no-op and the
    app must behave exactly as the RingClipTests above already verify."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)
        self.app.native_ring_clips = True

    def _clip_dial_calls(self):
        return [c for c in self.app.service_calls
                if c[0] in ("abb_welcome/arm_streaming", "camera/record")]

    def _native_clip_event(self, **overrides):
        payload = {
            "station_id": "100000002",
            "filename": "abb_ringclip_20260824_090010_100000002.mp4",
            "path": "/config/www/abb_doorbell/abb_ringclip_20260824_090010_100000002.mp4",
            "url": "/local/abb_doorbell/abb_ringclip_20260824_090010_100000002.mp4",
            "duration_s": 12.5,
            "frames": 300,
            "segments": 2,
            "started_at": self.clock.at.isoformat(),
            "reason": "ring",
            "ok": True,
        }
        payload.update(overrides)
        self.app._on_native_ring_clip("abb_welcome_ring_clip", payload, {})
        return payload

    # --- native gate (item 1: off means exactly as today) ---

    def test_native_off_schedules_start_clip(self):
        self.app.native_ring_clips = False
        self.app._open_episode("front door", "100000002", self.clock.now())
        scheduled = [cb.__name__ for cb, _, _ in self.app.run_in_calls]
        self.assertIn("_start_clip", scheduled)

    def test_native_on_never_schedules_start_clip(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        scheduled = [cb.__name__ for cb, _, _ in self.app.run_in_calls]
        self.assertNotIn("_start_clip", scheduled)

    def test_native_on_opens_self_call_window_at_episode_open(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        until = self.app._self_call_until.get("front door")
        self.assertIsNotNone(until)
        self.assertEqual((until - episode["started_at"]).total_seconds(), 45)

    def test_no_clip_camera_door_opens_no_self_call_window(self):
        self.app.clip_cameras = {}
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.assertNotIn("front door", self.app._self_call_until)

    def test_native_off_ignores_the_event_entirely(self):
        self.app.native_ring_clips = False
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self._native_clip_event()
        self.assertIsNone(episode["clip_filename"])

    # --- event -> episode clip attachment ---

    def test_matching_ring_event_attaches_clip(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self.clock.at += timedelta(seconds=15)
        payload = self._native_clip_event()
        self.assertEqual(episode["clip_filename"], payload["filename"])
        self.assertEqual(episode["clip_started_at"], bridge_mod.parse_iso_ts(payload["started_at"]))
        self.assertTrue(any("CLIP-NATIVE" in m for _, m in self.app.logs))
        self.assertEqual(self._clip_dial_calls(), [])  # never dials itself

    def test_service_reason_is_ignored(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self._native_clip_event(reason="service")
        self.assertIsNone(episode["clip_filename"])

    def test_ok_false_is_dropped(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self._native_clip_event(ok=False)
        self.assertIsNone(episode["clip_filename"])
        self.assertTrue(any("CLIP-NATIVE dropped" in m for _, m in self.app.logs))

    def test_no_open_episode_is_dropped(self):
        self._native_clip_event()  # nothing open for that station's door
        self.assertTrue(any("CLIP-NATIVE dropped" in m for _, m in self.app.logs))

    def test_unmapped_station_is_dropped(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self._native_clip_event(station_id="999999999")
        self.assertIsNone(episode["clip_filename"])

    def test_clip_attaches_to_the_matching_door_only(self):
        front = self.app._open_episode("front door", "100000002", self.clock.now())
        self.clock.at += timedelta(seconds=2)
        back = self.app._open_episode("back door", "100000001", self.clock.now())
        self._native_clip_event(station_id="100000001", filename="abb_ringclip_back.mp4")
        self.assertIsNone(front["clip_filename"])
        self.assertEqual(back["clip_filename"], "abb_ringclip_back.mp4")

    def test_native_clip_archives_like_the_fallback_clip(self):
        # The existing close/archive path needs nothing more than clip_filename -
        # same directory the fallback dial already wrote into.
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.app.archive_dir.mkdir(parents=True, exist_ok=True)
        payload = self._native_clip_event()
        (self.app.archive_dir / payload["filename"]).write_bytes(b"mp4data")
        self.clock.at += timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        entry = self.app._load_index()[0]
        self.assertEqual(entry["clip_filename"], payload["filename"])
        self.assertEqual(entry["clip_url"], f"/local/abb_doorbell/{payload['filename']}")

    # --- native voice retry ---

    def test_voice_chain_starts_after_the_ring_call_is_gone(self):
        """The sentence must land on the continuation dial, not the ring call.

        The station does not render audio on its own ring call (114 clean
        packets, three human nulls) but does on a call HA dialled. Starting at
        delay 0 meant play_audio was ACCEPTED by the ring call and the chain
        stopped on that silent success, never reaching the audible one.
        """
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.app._maybe_announce("front door")
        delays = [
            delay
            for cb, delay, _kw in self.app.run_in_calls
            if cb.__name__ == "_native_voice_retry"
        ]
        self.assertTrue(delays, "expected a voice chain to be scheduled")
        self.assertGreaterEqual(
            delays[0],
            self.app.voice_start_delay_s,
            "first attempt must wait for the ring call to end",
        )

    def test_native_voice_never_dials_announce(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.app._maybe_announce("front door")
        _run_scheduled(self.app, "_native_voice_retry")
        self.assertEqual([c for c in self.app.service_calls if c[0] == "abb_welcome/announce"], [])
        self.assertTrue(any(c[0] == "abb_welcome/play_audio" for c in self.app.service_calls))

    def test_play_audio_payload_carries_media_content_type(self):
        """REGRESSION 2026-08-25: the payload must satisfy the integration's schema.

        abb_welcome/play_audio declares `vol.Required("media"): MediaSelector(...)`,
        which requires BOTH media_content_id and media_content_type. We sent only
        the id, so HA rejected every call with
        `invalid_format: required key not provided @ data['media']['media_content_type']`
        - and because AppDaemon 4.5.13's HASS plugin exposes no return_result, the
        rejection arrived asynchronously as a websocket warning the app never saw.
        Five days of silent doors (2026-08-20 .. 08-25) reported as "succeeded".
        """
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.app._maybe_announce("front door")
        _run_scheduled(self.app, "_native_voice_retry")

        plays = [c for c in self.app.service_calls if c[0] == "abb_welcome/play_audio"]
        self.assertTrue(plays, "expected at least one play_audio call")
        for _service, kwargs in plays:
            media = kwargs.get("media")
            self.assertIsInstance(media, dict)
            self.assertIn("media_content_id", media)
            self.assertIn(
                "media_content_type",
                media,
                "play_audio without media_content_type is rejected by the "
                "integration and the rejection is invisible to this app",
            )
            self.assertTrue(str(media["media_content_type"]).strip())
            self.assertTrue(
                str(media["media_content_id"]).startswith("media-source://tts/")
            )

    def test_play_audio_rejection_is_seen_and_not_reported_as_spoken(self):
        """A rejected service call must not count as a spoken sentence.

        AppDaemon returns Home Assistant's whole websocket response from
        call_service - hassplugin.receive_result() resolves the future with
        `resp` before logging its own warning - so a rejection is visible here
        as {"success": False, "error": {...}}. Throwing that away is what let
        the 2026-08-20 malformed payload report "succeeded" for five days.
        """
        self.app.call_service = lambda service, **kwargs: {
            "id": 154,
            "type": "result",
            "success": False,
            "error": {"code": "invalid_format", "message": "required key not provided"},
            "ad_status": "OK",
        }
        spoken = self.app._voice_into_recording(
            "front door", "camera.abb_welcome_gateway_outdoor_station_2_1", "The door is open."
        )
        self.assertFalse(spoken, "a rejected play_audio must return False so the retry fires")

    def test_play_audio_success_counts_as_spoken(self):
        self.app.call_service = lambda service, **kwargs: {
            "id": 155,
            "type": "result",
            "success": True,
            "result": None,
            "ad_status": "OK",
        }
        spoken = self.app._voice_into_recording(
            "front door", "camera.abb_welcome_gateway_outdoor_station_2_1", "The door is open."
        )
        self.assertTrue(spoken)

    def test_native_voice_retries_until_success(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        calls = {"n": 0}

        def flaky(service, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("no talkback yet")
        self.app.call_service = flaky
        self.app._maybe_announce("front door")
        for _ in range(5):
            _run_scheduled(self.app, "_native_voice_retry")
        self.assertEqual(calls["n"], 3)
        self.assertTrue(episode["voice_spoken"])
        # idempotent: running any further pending attempts changes nothing
        _run_scheduled(self.app, "_native_voice_retry")
        self.assertEqual(calls["n"], 3)

    def test_native_voice_gives_up_after_five_failures(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())

        def boom(service, **kwargs):
            raise RuntimeError("still no talkback")
        self.app.call_service = boom
        self.app._maybe_announce("front door")
        attempts = 0
        for _ in range(6):
            attempts += _run_scheduled(self.app, "_native_voice_retry")
        self.assertEqual(attempts, 5)
        self.assertFalse(episode["voice_spoken"])

    def test_no_open_ring_stays_silent(self):
        self.app._maybe_announce("front door")  # no episode at all
        self.assertEqual(self.app.run_in_calls, [])

    def test_ring_outside_announce_window_stays_silent(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.clock.at += timedelta(seconds=self.app.announce_ring_window_s + 1)
        self.app._maybe_announce("front door")
        pending = [cb.__name__ for cb, _, _ in self.app.run_in_calls]
        self.assertNotIn("_native_voice_retry", pending)

    # --- voice dedup (one spoken sentence per episode, both paths) ---

    def test_two_unlock_confirms_speak_once_native(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        self.app._maybe_announce("front door")               # unlock attempt 1 confirms
        _run_scheduled(self.app, "_native_voice_retry")       # attempt 1 succeeds
        self.assertTrue(episode["voice_spoken"])
        n_before = len([c for c in self.app.service_calls if c[0] == "abb_welcome/play_audio"])
        self.assertEqual(n_before, 1)
        self.app._maybe_announce("front door")               # unlock attempt 3 confirms
        _run_scheduled(self.app, "_native_voice_retry")
        n_after = len([c for c in self.app.service_calls if c[0] == "abb_welcome/play_audio"])
        self.assertEqual(n_after, 1)  # unchanged - no second sentence

    def test_reentrant_maybe_announce_does_not_start_a_second_chain(self):
        # Two unlock attempts BOTH confirm before either has actually succeeded in
        # speaking - the exact double-fire scenario. voice_spoken alone cannot
        # guard this (it must stay false through several failing attempts for the
        # retry to have a point), so voice_dispatched stops the SECOND
        # _maybe_announce call from starting an independent chain of its own.
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        calls = {"n": 0}

        def boom(service, **kwargs):
            calls["n"] += 1
            raise RuntimeError("no talkback yet")
        self.app.call_service = boom
        self.app._maybe_announce("front door")  # unlock attempt 1 confirms
        self.app._maybe_announce("front door")  # unlock attempt 3 confirms, moments later
        for _ in range(6):
            _run_scheduled(self.app, "_native_voice_retry")
        self.assertEqual(calls["n"], 5)  # one chain's worth of dial attempts, not two
        self.assertFalse(episode["voice_spoken"])

    def test_two_unlock_confirms_speak_once_legacy_in_recording(self):
        # The pre-existing (native off) in-recording branch had the actual bug:
        # unlike the plain announce dial below it, it skipped the cooldown check
        # entirely, so two confirms fired two sentences.
        self.app.native_ring_clips = False
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        episode["clip_started_at"] = self.clock.at  # a recording is "in flight"
        self.app._maybe_announce("front door")  # unlock attempt 1 confirms
        self.app._maybe_announce("front door")  # unlock attempt 3 confirms
        calls = [c for c in self.app.service_calls if c[0] == "abb_welcome/play_audio"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(episode["voice_spoken"])


class RingArmedVoiceTests(unittest.TestCase):
    """The sentence starts at the RING, not after the unlock (2026-08-27).

    Mikkel's sequence is recording -> announcement -> unlock, and intercom.py
    holds the door for it. But
    the sentence hung off _maybe_announce, whose only trigger is the lock's own
    unlocking/unlocked edge, so it could not physically start until AFTER the door
    had opened. Measured on the 18:57 front-door ring that evening: door open at
    ring+3.9 s, sentence dispatched at ring+7.0 s. The hold bought nothing.
    """

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 27, 18, 57, 30, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)
        self.app.native_ring_clips = True
        self.app.voice_start_delay_s = 1.5
        self.app.states[("input_boolean.auto_open_intercom", None)] = "on"

    def _voice_delays(self):
        return [delay for cb, delay, _kw in self.app.run_in_calls
                if cb.__name__ == "_native_voice_retry"]

    def test_ring_arms_the_voice_before_the_unlock_lands(self):
        self.app._open_episode("front door", "100000002", self.clock.now())
        # The door now waits on the sentence's own ending, but the sentence
        # must still be dispatched inside the answered ring call (torn down at
        # ~ring+9 s) or there is no call left to speak into.
        self.assertEqual(self._voice_delays(), [1.5])

    def test_armed_chain_and_a_later_unlock_confirm_speak_once(self):
        episode = self.app._open_episode("front door", "100000002", self.clock.now())
        _run_scheduled(self.app, "_native_voice_retry")   # the ring-armed attempt 1
        self.assertTrue(episode["voice_spoken"])
        self.app._maybe_announce("front door")            # unlock edge, moments later
        _run_scheduled(self.app, "_native_voice_retry")
        calls = [c for c in self.app.service_calls if c[0] == "abb_welcome/play_audio"]
        self.assertEqual(len(calls), 1)

    def _spoken_events(self):
        return [kw for name, kw in self.app.fired_events
                if name == "abb_announcement_spoken"]

    def test_a_spoken_sentence_publishes_its_own_ending(self):
        """play_audio returns only after the integration has paced out every
        20 ms frame and drained the queue, so that return IS the sentence
        ending. intercom.py holds the door for exactly this event - without it
        the door falls back to a ceiling timer and the ordering is luck."""
        self.app._open_episode("front door", "100000002", self.clock.now())
        _run_scheduled(self.app, "_native_voice_retry")
        _run_scheduled(self.app, "_publish_voice_spoken")

        self.assertEqual(self._spoken_events(), [{"door": "front door"}])

    def test_a_refused_sentence_publishes_nothing(self):
        """A door held on an event that never comes must fall to the ceiling,
        not be released by a sentence nobody heard."""
        self.app.call_service = lambda service, **kw: {
            "success": False,
            "error": {"code": "x", "message": "in use"},
        }
        self.app._open_episode("front door", "100000002", self.clock.now())
        _run_scheduled(self.app, "_native_voice_retry")
        _run_scheduled(self.app, "_publish_voice_spoken")

        self.assertEqual(self._spoken_events(), [])

    def test_publishing_the_ending_never_raises(self):
        """Strictly additive, like every other emitter here: a lost event costs
        the ceiling wait, never the door."""
        def boom(*a, **kw):
            raise RuntimeError("event bus down")
        self.app.fire_event = boom
        self.app._publish_voice_spoken({"door": "front door"})  # must not raise

    def test_auto_open_off_leaves_the_unlock_trigger_in_charge(self):
        # Nobody has promised the door will open yet - a human still has to press
        # Open on the ring push. Announcing "Door is opening." at the ring would
        # be a lie the house cannot keep.
        self.app.states[("input_boolean.auto_open_intercom", None)] = "off"
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.assertEqual(self._voice_delays(), [])

    def test_door_without_an_announce_camera_stays_silent(self):
        # The back door has its own hardware voice module; two overlapping
        # voices are worse than none.
        self.app._open_episode("back door", "100000001", self.clock.now())
        self.assertEqual(self._voice_delays(), [])

    def test_native_off_does_not_arm_at_the_ring(self):
        # The legacy path dials a temporary call for the announce and must keep
        # yielding to the recording exactly as before.
        self.app.native_ring_clips = False
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.assertEqual(self._voice_delays(), [])

    def test_empty_announce_message_arms_nothing(self):
        self.app.announce_message = ""
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.assertEqual(self._voice_delays(), [])

    def test_unreadable_auto_open_stays_silent(self):
        # _auto_open_on() treats unreadable as off; arming on a door that may
        # never open is the one failure mode that reaches the street.
        self.app.states.pop(("input_boolean.auto_open_intercom", None), None)
        self.app._open_episode("front door", "100000002", self.clock.now())
        self.assertEqual(self._voice_delays(), [])


class DoorOpenFeedTests(unittest.TestCase):
    """Street-door open feed (2026-08-25, module docstring item 5): every ESP
    unlock edge reports ONE house_events_report, classified by whether a ring
    was present, with same-visitor edges folded into a single report."""

    def setUp(self):
        self.clock = Clock(datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _bare_bridge(self.tmp.name, self.clock)

    def _door_open_reports(self):
        return [kwargs for name, kwargs in self.app.fired_events if name == "house_events_report"]

    def _unlock(self, entity="lock.intercomproxy_front_door", old="locked", new="unlocking"):
        self.app._on_lock_activity(entity, "state", old, new, {})

    def test_opening_during_a_ring_emits_ring_wording(self):
        self.app._register_ring("esp", "front door", "", self.clock.at, "esp:test")
        self.clock.at += timedelta(seconds=5)
        self._unlock()
        reports = self._door_open_reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["cause"], "Someone rang the front door")
        self.assertEqual(reports[0]["effect"], "The door was opened for them")
        self.assertEqual(reports[0]["icon"], "mdi:door-open")

    def test_opening_with_no_ring_emits_no_ring_wording(self):
        self._unlock()
        reports = self._door_open_reports()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["cause"], "The front door was opened with nobody ringing")
        self.assertEqual(reports[0]["effect"],
                         "Possibly a buzz-in for someone who called ahead, or a test")
        self.assertEqual(reports[0]["icon"], "mdi:door-open")

    def test_edges_inside_fold_window_emit_one_report(self):
        self._unlock(new="unlocking")
        self.clock.at += timedelta(seconds=5)
        self._unlock(old="unlocking", new="unlocked")
        self.clock.at += timedelta(seconds=10)  # +15s from the 1st edge, +10s from the 2nd: inside fold_s=20
        self._unlock(new="unlocking")  # an auto-open retry
        self.assertEqual(len(self._door_open_reports()), 1)

    def test_edges_outside_fold_window_emit_two(self):
        self._unlock(new="unlocking")
        self.clock.at += timedelta(seconds=25)  # outside door_open_fold_s=20
        self._unlock(new="unlocking")
        self.assertEqual(len(self._door_open_reports()), 2)

    def test_feed_disabled_emits_nothing(self):
        self.app.door_open_feed = False
        self._unlock()
        self.clock.at += timedelta(seconds=30)
        self._unlock()
        self.assertEqual(self._door_open_reports(), [])

    def test_opening_shortly_after_ring_closed_still_counts_as_ring(self):
        # episode_close_s (75s, default) pops the episode out of self.episodes
        # entirely - door_open_ring_window_s (90s) must still find the ring via
        # _last_ring_closed_at.
        self.app._register_ring("esp", "front door", "", self.clock.at, "esp:test")
        self.clock.at += timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        self.clock.at += timedelta(seconds=10)  # ring+86s, inside the 90s window
        self._unlock()
        self.assertEqual(self._door_open_reports()[0]["cause"], "Someone rang the front door")

    def test_opening_long_after_ring_closed_no_longer_counts(self):
        self.app._register_ring("esp", "front door", "", self.clock.at, "esp:test")
        self.clock.at += timedelta(seconds=76)
        _run_scheduled(self.app, "_close_episode")
        self.clock.at += timedelta(seconds=20)  # ring+96s, outside the 90s window
        self._unlock()
        self.assertEqual(self._door_open_reports()[0]["cause"],
                         "The front door was opened with nobody ringing")

    def test_fire_event_exception_is_swallowed_and_announce_still_runs(self):
        self.app._open_episode("front door", "100000002", self.clock.at)
        self.clock.at += timedelta(seconds=5)

        def boom(event, **kwargs):
            raise RuntimeError("feed down")
        self.app.fire_event = boom
        self._unlock()
        announces = [c for c in self.app.service_calls if c[0] == "abb_welcome/announce"]
        self.assertEqual(len(announces), 1)
        self.assertTrue(any("Door-open feed report failed" in m for _, m in self.app.logs))
