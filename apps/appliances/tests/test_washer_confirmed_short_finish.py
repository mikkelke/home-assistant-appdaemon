# tests/test_washer_confirmed_short_finish.py - a short confirmed wash with no anti-crease phase
# (2026-09-23 Uld 30C, 39 min, one heating burst, 0.202 kWh) must be announced once, on time.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# It was never announced at the time: a 100-minute warm floor and a 3-recorder-point tail rule
# both blocked it. Neither exists any more - finish decisions read the plug directly and carry
# no programme-duration guards. The replay drives the real WasherMonitor end to end
# (make_full_init_app, see test_washer_restart_survival.py) against the recorded cycle in
# tests/fixtures/washer_uld_2026_09_23.json, with a heap-timed run_in so the recurring
# _check_energy_finish tick fires for real and the plug reads step-held on the 2 s grid.

from __future__ import annotations

import heapq
import itertools
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_washer_restart_survival as trs  # noqa: E402  (installs the appdaemon stub)

import washer_plug as wplug  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "washer_uld_2026_09_23.json"


def _production_args():
    """The real washer.yaml config (minus AppDaemon app-wiring keys and feedback_file, which
    must stay pointed at make_full_init_app's own throwaway path) - same set the validated
    exploration driver used, so this replay sees the exact tuning production runs with."""
    path = Path(__file__).resolve().parents[1] / "washer.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)["washer_monitor"]
    return {k: v for k, v in cfg.items() if k not in ("module", "class", "log", "dependencies", "feedback_file")}


class TestConfirmedUldReplay(unittest.TestCase):
    """End-to-end replay of the real 2026-09-23 Uld cycle through make_full_init_app: one
    on-time Unemptied (standby) and one Sonos call."""

    ENTITY_OF = {
        "power": "sensor.washer_plug_power",
        "energy": "sensor.washer_plug_energy",
        "door": "binary_sensor.washer_door_contact",
        "confirmed_programme": "input_select.washer_confirmed_programme",
        "temperature": "input_select.washer_temperature",
    }

    def _run(self):
        with open(FIXTURE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        t_start = datetime.fromisoformat(data["t_start"])
        t_stop = datetime.fromisoformat(data["t_stop"])
        series = {
            self.ENTITY_OF[name]: [(datetime.fromisoformat(t), s) for t, s in evs]
            for name, evs in data["events"].items()
        }

        app = trs.make_full_init_app(
            sensor_state="Off",
            helper_state="Off",
            power_watts=float(data["initial"]["power"]),
            now=t_start,
            extra_args=_production_args(),
        )
        app.states[self.ENTITY_OF["energy"]] = data["initial"]["energy"]
        app.states[self.ENTITY_OF["door"]] = data["initial"]["door"]
        app.states[self.ENTITY_OF["confirmed_programme"]] = data["initial"]["confirmed_programme"]
        app.states[self.ENTITY_OF["temperature"]] = data["initial"]["temperature"]
        app.states["input_boolean.washer_announce"] = "on"

        def get_history(entity_id=None, start_time=None, end_time=None, **kw):
            if entity_id not in series:
                return [[]]
            st = start_time.astimezone(timezone.utc)
            en = (end_time or app.now).astimezone(timezone.utc)
            out, carried = [], None
            for tt, s in series[entity_id]:
                if tt <= st:
                    carried = s
                elif tt <= en:
                    out.append({"state": s, "last_changed": tt.isoformat()})
            if carried is not None:
                out.insert(0, {"state": carried, "last_changed": st.isoformat()})
            return [out]

        app.get_history = get_history

        # Heap-timed run_in/cancel_timer: re-arm whatever initialize() already scheduled with
        # make_full_init_app's inert stub, then let every later self.run_in() land on the same
        # heap so the recurring _check_energy_finish tick keeps firing for the rest of the replay.
        seq = itertools.count()
        timers = {}
        heap = []

        def run_in(cb, delay, **kw):
            handle = next(seq)
            due = app.now + timedelta(seconds=float(delay))
            timers[handle] = (due, cb, kw)
            heapq.heappush(heap, (due, handle))
            return handle

        app.run_in = run_in
        app.timer_running = lambda handle: handle in timers
        app.cancel_timer = lambda handle: timers.pop(handle, None)
        for cb, delay, kw in list(app.scheduled):
            run_in(cb, delay, **kw)

        app.sonos_calls = []
        app.sonos_notifier = types.SimpleNamespace(
            notify=lambda message=None, **kw: app.sonos_calls.append((app.now, message))
        )
        app.push_calls = []
        app._push_mobile = lambda message: app.push_calls.append((app.now, message))

        # Edge-detect the real transition (not every subsequent same-state republish).
        real_transition_to_unemptied = app._transition_to_unemptied
        state_entity = app.args["state_entity"]
        self.transitions = []

        def wrapped_transition_to_unemptied(*a, **kw):
            was = app.state
            result = real_transition_to_unemptied(*a, **kw)
            if was != "Unemptied" and app.state == "Unemptied":
                end_reason = (app.attrs_store.get(state_entity) or {}).get("end_reason")
                self.transitions.append((app.now, end_reason))
            return result

        app._transition_to_unemptied = wrapped_transition_to_unemptied

        def fire_timers(upto):
            while heap and heap[0][0] <= upto:
                due, handle = heapq.heappop(heap)
                if handle not in timers or timers[handle][0] != due:
                    continue
                _, cb, kw = timers.pop(handle)
                app.now = due
                cb(kw)

        power = series[self.ENTITY_OF["power"]]

        def read():
            held = [s for tt, s in power if tt <= app.now]
            if not held or held[-1] in ("unknown", "unavailable"):
                raise TimeoutError("plug not reporting")
            return float(held[-1])

        app._plug._read = read
        app._plug.clock = lambda: (app.now - t_start).total_seconds()
        app._plug._last_ok = 0.0

        listeners = {
            self.ENTITY_OF["power"]: app._power_changed,
            self.ENTITY_OF["door"]: app._door_state_changed,
            self.ENTITY_OF["confirmed_programme"]: app._on_confirm_changed,
            self.ENTITY_OF["temperature"]: app._on_confirm_changed,
        }
        events = sorted(
            (tt, ent, s) for ent, evs in series.items() for tt, s in evs if t_start < tt <= t_stop
        )
        t = t_start + timedelta(seconds=wplug.POLL_S)
        while t <= t_stop:
            events.append((t, "__read__", None))
            t += timedelta(seconds=wplug.POLL_S)
        events.sort(key=lambda e: e[0])
        for tt, ent, s in events:
            fire_timers(tt)
            app.now = tt
            if ent == "__read__":
                app._plug.poll_once()
                continue
            old = app.states.get(ent)
            app.states[ent] = s
            if ent in listeners and old != s:
                listeners[ent](ent, "state", old, s, {})
        fire_timers(t_stop)
        return app

    def test_announces_once_via_standby_in_window(self):
        app = self._run()

        self.assertEqual(len(self.transitions), 1, self.transitions)
        transition_time, end_reason = self.transitions[0]
        self.assertEqual(end_reason, "standby")

        # 11:08:26-11:13:00 local (Europe/Copenhagen, +02) = 09:08:26-09:13:00 UTC.
        window_start = datetime(2026, 9, 23, 9, 8, 26, tzinfo=timezone.utc)
        window_end = datetime(2026, 9, 23, 9, 13, 0, tzinfo=timezone.utc)
        self.assertGreaterEqual(transition_time, window_start)
        self.assertLessEqual(transition_time, window_end)

        self.assertEqual(len(app.sonos_calls), 1, app.sonos_calls)
        self.assertEqual(app.push_calls, [])


if __name__ == "__main__":
    unittest.main()
