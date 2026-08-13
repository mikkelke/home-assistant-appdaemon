# tests/test_dishwasher_restart_resume.py - restart-safe cycle reconstruction from recorder
# history (the 2026-08-12 incident), replayed end to end against the REAL initialize().
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# The incident: the Miele G5050's ECO ends with a PASSIVE AutoOpen drying phase that draws
# literally 0.0 W for over an hour while the machine's display still counts down. Three HA
# restarts (13:19, 13:24, ~14:37) wiped sensor.dishwasher_state; the 14:38:14 re-init looked
# only at instantaneous power (0.0 W, mid-dry), concluded "nothing running", and the 11:38
# cycle - 131 heater bursts >1 kW, 0.65 kWh, active 11:38-13:18 - would have ended SILENTLY
# in Off. The fix: on init with the entity missing/Off, reconstruct from the plug's power
# (and energy) HISTORY; the cycle start is the first sample of the activity cluster - never
# the entity's last_changed, which after a wipe is merely the restart re-creating the entity
# (the washer's 13:24 mis-anchor, covered in test_washer_restore_wipe_anchor.py).
#
# Per this repo's incident history, the replay runs the REAL initialize(),
# _maybe_resume_cycle_from_history, _confirm_finished, _finish_with_dry_tail,
# _dry_tail_elapsed, _transition_to_unemptied and _save_cycle_feedback. Only the AppDaemon
# I/O boundary (get_state / set_state / get_history / run_in / timer_running / cancel_timer /
# listen_* / call_service / get_app / log) is faked - test_dryer_emptied_exit.py's harness
# style. Timestamps mirror the incident's local clock but are labelled UTC in the fixture;
# only deltas matter to the logic under test.

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

UTC = timezone.utc
START = datetime(2026, 8, 12, 11, 38, 17, tzinfo=UTC)      # real cycle start
ACTIVE_END = datetime(2026, 8, 12, 13, 18, 10, tzinfo=UTC)  # power flat 0.0 W from here
INIT_AT = datetime(2026, 8, 12, 14, 38, 14, tzinfo=UTC)     # the app re-init that lost the cycle
# Learned eco 214 min (recorded 07:13 that morning) x the deployed 92% guard fraction:
GUARD_OPEN = START + timedelta(minutes=214 * 0.92)          # ~14:55:10
MACHINE_END = datetime(2026, 8, 12, 15, 26, 0, tzinfo=UTC)  # display said 37-38 min left at 14:49

REPO_APPLIANCES = Path(__file__).resolve().parents[1]


def _iso(dt):
    return dt.astimezone(UTC).isoformat()


def incident_power_history():
    """The plug's recorder series for today: idle, 131 heater bursts 11:40-13:15, a last pump
    sample, then hard 0.0 W through the AutoOpen dry."""
    entries = [
        (START - timedelta(minutes=18), "0.0"),  # idle before the start (window not truncated)
        (START - timedelta(minutes=8), "0.0"),
        (START, "20.0"),                          # pump: the first sample of the activity cluster
        (START + timedelta(seconds=30), "18.0"),
    ]
    burst_t = START + timedelta(minutes=2)
    for _ in range(131):
        entries.append((burst_t, "1800.0"))
        entries.append((burst_t + timedelta(seconds=22), "1.0"))
        burst_t += timedelta(seconds=44)
    entries.append((ACTIVE_END - timedelta(seconds=20), "15.0"))  # final pump blip
    entries.append((ACTIVE_END, "0.0"))
    entries.append((ACTIVE_END + timedelta(minutes=22), "0.0"))
    entries.append((INIT_AT - timedelta(minutes=8), "0.0"))
    return [{"last_changed": _iso(t), "state": s} for t, s in entries]


def incident_energy_history():
    """Cumulative kWh: 1.00 at start, 1.65 when the active phase ends (0.65 kWh cycle)."""
    entries = [
        (START - timedelta(minutes=18), "1.00"),
        (START, "1.00"),
        (START + timedelta(minutes=22), "1.20"),
        (START + timedelta(minutes=52), "1.40"),
        (START + timedelta(minutes=82), "1.55"),
        (ACTIVE_END, "1.65"),
        (INIT_AT - timedelta(minutes=8), "1.65"),
    ]
    return [{"last_changed": _iso(t), "state": s} for t, s in entries]


class FakeSonos:
    def __init__(self):
        self.calls = []

    def notify(self, message):
        self.calls.append(message)


def make_incident_app(tmpdir, *, init_at=INIT_AT, helper_state="Running",
                      power_history=None, energy_history=None):
    """DishwasherMonitor whose initialize() runs FOR REAL at init_at, with the incident's
    recorder history behind get_history and a wiped state entity (HA restart)."""
    feedback_path = os.path.join(tmpdir, "dishwasher_feedback.json")
    with open(feedback_path, "w") as f:
        json.dump(
            {
                "version": 2,
                # Five confirmed ECO cycles at 214 min -> learned eco avg 214 (n=5), matching
                # the learned duration recorded 07:13 on the day of the incident.
                "cycles": [
                    {
                        "ts": "2026-08-0%d 09:00:00" % (i + 1),
                        "duration_min": 214,
                        "energy_kwh": 0.65,
                        "predicted": "eco",
                        "confirmed": "eco",
                        "programme_confirmed_by_human": True,
                        "max_power_w": 1800,
                        "short": False,
                    }
                    for i in range(5)
                ],
            },
            f,
        )

    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)
    app.args = {
        "power_sensor": "sensor.athom_smart_plug_v3_50ec44_power",
        "energy_sensor": "sensor.athom_smart_plug_v3_50ec44_energy",
        "door_sensor": "binary_sensor.dishwasher_door_contact",
        "state_entity": "sensor.dishwasher_state",
        "start_w": 8,
        "stop_w": 2,
        "run_for": 60,
        "stop_for": 90,
        "min_energy_kwh": 0.4,
        # The deployed guard opened at ~92% of learned 214 -> ~14:55 (the incident numbers).
        "finish_guard_fraction": 0.92,
        # The REAL programmes yaml: the replay must see eco's dry_tail_minutes: 35 from the
        # checked-in file, not a fixture copy.
        "programmes_file": str(REPO_APPLIANCES / "dishwasher_programmes.yaml"),
        "feedback_file": feedback_path,
        # Scope the durable cycle store to tmpdir like feedback_file above. Without this the
        # app defaults to apps/appliances/dishwasher_cycle_state.json - a REAL file next to the
        # source - so one run left a store behind that the next run's boot resolution then read
        # back, making these tests order-dependent and failing five of them. Harmless before
        # the store existed; load-bearing now that it sits in the resolution path.
        "state_file": str(Path(tmpdir) / "dishwasher_cycle_state.json"),
    }

    app.now = init_at
    app._now_utc = lambda: app.now

    app.states = {
        "sensor.dishwasher_state": None,  # wiped by the HA restart
        "input_select.dishwasher_state": helper_state,  # the mirror survived
        "sensor.athom_smart_plug_v3_50ec44_power": "0.0",  # mid-dry: literally nothing
        "sensor.athom_smart_plug_v3_50ec44_energy": "1.65",
        "binary_sensor.dishwasher_door_contact": "off",
    }
    app.attrs = {}
    app.entity_last_changed = {}

    def get_state(entity, **kw):
        attr = kw.get("attribute")
        if attr == "all":
            if app.states.get(entity) is None and entity not in app.attrs:
                return None
            return {
                "attributes": dict(app.attrs.get(entity, {})),
                "last_changed": app.entity_last_changed.get(entity),
            }
        if attr:
            return app.attrs.get(entity, {}).get(attr)
        return app.states.get(entity)

    app.get_state = get_state

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
        app.entity_last_changed[entity] = _iso(app.now)

    app.set_state = set_state

    histories = {
        "sensor.athom_smart_plug_v3_50ec44_power": power_history if power_history is not None else incident_power_history(),
        "sensor.athom_smart_plug_v3_50ec44_energy": energy_history if energy_history is not None else incident_energy_history(),
    }
    app.history_calls = []

    def get_history(**kw):
        app.history_calls.append(kw)
        return histories.get(kw.get("entity_id"), [])

    app.get_history = get_history

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.listen_state = lambda *a, **kw: None
    app.listen_event = lambda *a, **kw: None
    app.service_calls = []
    app.call_service = lambda *a, **kw: app.service_calls.append((a, kw))
    app.fake_sonos = FakeSonos()
    app.get_app = lambda name: app.fake_sonos if name == "SonosNotifier" else None

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


def logged(app, needle):
    return any(needle in str(a[0]) for a, _kw in app.log_calls)


class TodayEndToEnd(unittest.TestCase):
    """init at 14:38-equivalent with 0 W instantaneous but a 131-burst history 11:38-13:18 ->
    Running restored with start 11:38, then guards + dry tail -> Unemptied at the machine's
    own end (~15:26-15:30), announced - instead of the silent Off the incident produced."""

    def setUp(self):
        self._orig_profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig_profiles
        self._tmp.cleanup()

    def test_full_replay(self):
        app = make_incident_app(self._tmp.name)
        app.initialize()

        # --- Phase 1: init at 14:38. Restore truth comes from HISTORY, not the 0.0 W read.
        self.assertEqual(app.state, "Running")
        self.assertEqual(app.states["sensor.dishwasher_state"], "Running")
        # Cycle start = first sample of the activity cluster in the recorder - never the
        # entity's last_changed (the entity did not even exist at init).
        self.assertEqual(app.start_time, START)
        self.assertEqual(app.energy_start, 1.0)
        self.assertEqual(app.attrs["sensor.dishwasher_state"].get("restored_from"), "power_history")
        self.assertTrue(logged(app, "Restart-safe resume"))
        # No finish, no announcement yet - the guard has not opened.
        self.assertEqual(app.fake_sonos.calls, [])

        # --- Phase 2: the guard opens (~14:55) and the poll/low-power path confirms the
        # finish at ~14:56:41. Under the old code this announced Unemptied right here,
        # ~30-40 min before the machine's own countdown reached zero.
        t_met = datetime(2026, 8, 12, 14, 56, 41, tzinfo=UTC)
        app.now = t_met
        app._confirm_finished({})

        self.assertEqual(app.states["sensor.dishwasher_state"], "Running")  # NOT Unemptied yet
        self.assertEqual(app.fake_sonos.calls, [])  # announcement moved, not dropped
        self.assertIsNotNone(app.dry_tail_timer)
        pending = app._dry_tail_pending
        self.assertIsNotNone(pending)
        target = pending["target"]
        # Target = guard-open (~14:55:10) + eco dry_tail 35 min ~= 15:30 - at/after the
        # machine's own ~15:26 end, never before it.
        self.assertGreaterEqual(target, MACHINE_END)
        self.assertLessEqual(target, MACHINE_END + timedelta(minutes=9))
        tail_delays = [d for cb, d, _kw in app.scheduled if getattr(cb, "__name__", "") == "_dry_tail_elapsed"]
        self.assertEqual(len(tail_delays), 1)
        self.assertAlmostEqual(tail_delays[0], (target - t_met).total_seconds(), delta=2)

        # Re-entry while the tail is pending (the 60s poll keeps re-arming the low-power
        # timer during the dry) must not double-schedule or transition.
        app.now = t_met + timedelta(minutes=2)
        app._confirm_finished({})
        tail_delays = [d for cb, d, _kw in app.scheduled if getattr(cb, "__name__", "") == "_dry_tail_elapsed"]
        self.assertEqual(len(tail_delays), 1)
        self.assertEqual(app.states["sensor.dishwasher_state"], "Running")

        # --- Phase 3: the tail elapses at the machine's own end -> Unemptied + announcement.
        app.now = target
        app._dry_tail_elapsed({})

        self.assertEqual(app.states["sensor.dishwasher_state"], "Unemptied")
        self.assertEqual(app.fake_sonos.calls, ["Dishwasher is ready to be emptied"])

        # Feedback records the guard-time run_minutes (~198), NOT target-start (~232): the
        # learned-duration series must not silently absorb the tail, or next run's guard
        # (and therefore next run's tail target) ratchets later every cycle.
        with open(app.feedback_file) as f:
            cycles = json.load(f)["cycles"]
        last = cycles[-1]
        expected_run = (t_met - START).total_seconds() / 60
        self.assertAlmostEqual(last["duration_min"], expected_run, delta=1.0)
        self.assertLess(last["duration_min"], 210)  # far from the 232 a tail-absorbing bug would record
        self.assertEqual(last["confirmed"], "eco")
        self.assertEqual(last["end_reason"], "low_power_detected")
        self.assertAlmostEqual(last["energy_kwh"], 0.65, delta=0.01)


class ResumeGuards(unittest.TestCase):
    """The resume must fire only for a cycle whose finish decision is genuinely still
    pending - concluded/stale/noise shapes stay Off."""

    def setUp(self):
        self._orig_profiles = dm.DishwasherMonitor.PROGRAMME_PROFILES
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        dm.DishwasherMonitor.PROGRAMME_PROFILES = self._orig_profiles
        self._tmp.cleanup()

    def test_concluded_cycle_shape_stays_off(self):
        """Init long after guards+dry+grace have passed (16:30 vs deadline ~15:45): the cycle
        is treated as concluded - someone may already have emptied it unseen."""
        app = make_incident_app(self._tmp.name, init_at=datetime(2026, 8, 12, 16, 30, 0, tzinfo=UTC))
        app.initialize()
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.states["sensor.dishwasher_state"], "Off")
        self.assertIsNone(app.start_time)
        self.assertTrue(logged(app, "treating as concluded"))
        self.assertEqual(app.fake_sonos.calls, [])

    def test_no_cycle_signature_stays_off(self):
        """A short 15 W blip with no heater bursts and ~0 kWh is not a cycle."""
        blip_t = INIT_AT - timedelta(minutes=33)
        power = [
            {"last_changed": _iso(blip_t - timedelta(minutes=5)), "state": "0.0"},
            {"last_changed": _iso(blip_t), "state": "15.0"},
            {"last_changed": _iso(blip_t + timedelta(seconds=60)), "state": "14.0"},
            {"last_changed": _iso(blip_t + timedelta(seconds=120)), "state": "0.0"},
        ]
        energy = [
            {"last_changed": _iso(blip_t - timedelta(minutes=5)), "state": "1.00"},
            {"last_changed": _iso(blip_t + timedelta(seconds=120)), "state": "1.02"},
        ]
        app = make_incident_app(self._tmp.name, power_history=power, energy_history=energy)
        app.initialize()
        self.assertEqual(app.state, "Off")
        self.assertTrue(logged(app, "lacks a cycle signature"))

    def test_activity_truncated_by_window_edge_stays_off(self):
        """History already active at the window edge: the true start is unknowable and the
        cycle is older than the window - do not resume with a too-late inferred start."""
        power = [
            {"last_changed": _iso(INIT_AT - timedelta(hours=4, minutes=59)), "state": "1800.0"},
            {"last_changed": _iso(INIT_AT - timedelta(hours=4, minutes=58)), "state": "1700.0"},
            {"last_changed": _iso(INIT_AT - timedelta(hours=4, minutes=57)), "state": "1.0"},
            {"last_changed": _iso(INIT_AT - timedelta(minutes=30)), "state": "0.0"},
        ]
        app = make_incident_app(self._tmp.name, power_history=power)
        app.initialize()
        self.assertEqual(app.state, "Off")
        self.assertTrue(logged(app, "truncated by window edge"))

    def test_mirror_saying_off_blocks_resume(self):
        """The input_select mirror survives HA restarts; a definitive Off there means a live
        app concluded this cycle (user emptied) - resuming would re-announce a done wash."""
        app = make_incident_app(self._tmp.name, helper_state="Off")
        app.initialize()
        self.assertEqual(app.state, "Off")
        self.assertEqual(app.fake_sonos.calls, [])
        self.assertIsNone(app.start_time)


if __name__ == "__main__":
    unittest.main()
