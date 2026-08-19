# tests/test_dryer_running_watchdog_paused.py - The 5h running watchdog must also clear a
# wedged Paused, not just a wedged Running.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# _running_watchdog_timeout is armed once, in _confirm_running, and is deliberately never
# cancelled by _transition_to_paused (see that method's docstring, and _restore_running_state's:
# "the 5h running watchdog spans Running AND any Paused time within it") - so the SAME timer
# armed while Running is still the one pending while Paused. Before this fix, the callback only
# ever checked `current_state == "Running"`, so a dryer stuck in Paused (e.g. a missed
# door-close edge - see test_dryer_pause_exit.py for that failure mode) had no backstop at all
# once the timer fired: it found state != "Running" and silently did nothing.
#
# Per this repo's incident history (see test_dryer_restart_survival.py's own docstring),
# stubbing the very interaction under test hides exactly this class of bug: a test that seeds
# running_watchdog_timer by hand and calls _running_watchdog_timeout({}) directly cannot tell
# "the callback ignores Paused" apart from "the callback was never armed for Paused in the first
# place" - both look identical if the arming side is also faked. So this drives the real
# sequence instead - _confirm_running arms the watchdog for real, a real door-open event
# (_door_state_changed) pauses the cycle without touching that timer - and only then pulls the
# ACTUAL scheduled callback back out of the fake run_in() log and invokes it, the same way
# AppDaemon's own scheduler would.

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

import dryer_monitor as dm  # noqa: E402

STATE_ENTITY = "sensor.dryer_state"
UI_SELECT = "input_select.dryer_state"
POWER_SENSOR = "sensor.dryer_plug_power"
ENERGY_SENSOR = "sensor.dryer_plug_energy"
DOOR_SENSOR = "binary_sensor.dryer_door_contact"


def _entity_get_state(entities):
    """get_state() double distinguishing attribute="all" from a bare call - same shape as
    test_dryer_restart_survival.py's / test_dryer_duplicate_feedback.py's, needed because the
    real initialize() boot-resolution path reads the state entity both ways."""

    def get_state(entity, attribute=None, **kwargs):
        rec = entities.get(entity)
        if rec is None:
            return None
        if attribute == "all":
            return {
                "state": rec.get("state"),
                "attributes": dict(rec.get("attributes") or {}),
                "last_changed": rec.get("last_changed"),
                "last_updated": rec.get("last_changed"),
            }
        if attribute:
            return (rec.get("attributes") or {}).get(attribute)
        return rec.get("state")

    return get_state


def make_app(tmpdir, *, power_w="0", energy_kwh="10.0", door_state="off"):
    """Real DryerMonitor with initialize() run for real - same tmpdir-scoping discipline as
    test_dryer_restart_survival.py's / test_dryer_duplicate_feedback.py's make_app (state_file/
    feedback_file/programmes_file all live under `tmpdir`, so nothing here ever touches, or
    races on, the real apps/appliances/dryer_*.json files). Boots straight to Off (no sensor/
    helper state), then each test drives _confirm_running/_door_state_changed for real - see the
    module docstring above for why neither the restore path nor _set_state_entity is stubbed."""
    app = dm.DryerMonitor.__new__(dm.DryerMonitor)

    entities = {
        STATE_ENTITY: {"state": None, "attributes": {}, "last_changed": None},
        UI_SELECT: {"state": None, "attributes": {}, "last_changed": None},
        POWER_SENSOR: {"state": power_w, "attributes": {}, "last_changed": None},
        ENERGY_SENSOR: {"state": energy_kwh, "attributes": {}, "last_changed": None},
        DOOR_SENSOR: {"state": door_state, "attributes": {}, "last_changed": None},
    }

    app.get_state = _entity_get_state(entities)
    app.get_history = lambda **kwargs: []

    state_file = str(Path(tmpdir) / "dryer_cycle_state.json")
    app.args = {
        "power_sensor": POWER_SENSOR,
        "energy_sensor": ENERGY_SENSOR,
        "door_sensor": DOOR_SENSOR,
        "state_entity": STATE_ENTITY,
        "ui_state_entity": UI_SELECT,
        "start_w": 8,
        "stop_w": 5,
        "run_for": 60,
        "stop_for": 60,
        "max_running_hours": 5,
        "feedback_file": str(Path(tmpdir) / "dryer_feedback_test.json"),
        "programmes_file": str(Path(tmpdir) / "dryer_programmes_test.yaml"),
        "state_file": state_file,
    }
    app.state_file = state_file

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

    def set_state(entity_id, **kw):
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


def _scheduled_callback(app, target):
    """The actual callback run_in recorded for `target` (a bound method) - not a fresh reference
    to it. See the module docstring for why firing THIS, rather than calling `target` by name, is
    the point of this test."""
    for cb, _delay, _kw in app.scheduled:
        if cb == target:
            return cb
    return None


class RunningWatchdogAlsoClearsAWedgedPaused(unittest.TestCase):
    """The 5h running watchdog is armed once (in _confirm_running) and deliberately never
    cancelled on pause (_transition_to_paused's own docstring, and _restore_running_state's -
    see the module docstring above), so the timer that eventually fires is the SAME one armed
    back when the cycle started Running. It must clear Paused exactly like it already clears
    Running - un-forced, matching the Running branch (max_running_hours dwarfs cooling_period in
    practice, so no force=True is needed here)."""

    def test_watchdog_fires_while_paused_and_forces_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, power_w="0")
            app.initialize()
            self.assertEqual(app.state, "Off")

            t0 = app._now_utc()
            clock = {"now": t0}
            app._now_utc = lambda: clock["now"]

            # Start a real cycle - arms the running watchdog for real, via _confirm_running.
            entities[POWER_SENSOR]["state"] = "900"  # >= start_w
            app._confirm_running({})
            self.assertEqual(app.state, "Running")
            watchdog_cb = _scheduled_callback(app, app._running_watchdog_timeout)
            self.assertIsNotNone(watchdog_cb, "expected the running watchdog to be armed by _confirm_running")

            # Real door-open with power still high -> Paused (checking laundry), via the real
            # listen_state callback. 15 real minutes past cycle start clears cooling_period so
            # the Paused transition itself is not refused.
            clock["now"] = t0 + timedelta(minutes=15)
            entities[DOOR_SENSOR]["state"] = "on"
            app._door_state_changed(DOOR_SENSOR, None, "off", "on", {})
            self.assertEqual(app.state, "Paused")
            still_armed = [cb for cb, _d, _kw in app.scheduled if cb == app._running_watchdog_timeout]
            self.assertEqual(len(still_armed), 1, "_transition_to_paused must not cancel or re-arm the running watchdog")

            # Another 15 real minutes on, clearing cooling_period again so the watchdog's own
            # _transition_to_off is not itself refused (max_running_hours is measured in hours,
            # so in production this backstop always fires far past any cooling window).
            clock["now"] = t0 + timedelta(minutes=30)

            # Fire the SAME callback run_in actually recorded - not a fresh call to the method by
            # name (see the module docstring: that alone cannot distinguish "ignores Paused" from
            # "was never armed for Paused" in the first place).
            watchdog_cb({})

            self.assertEqual(app.state, "Off")
            self.assertEqual(entities[STATE_ENTITY]["state"], "Off")
            self.assertIsNone(app.running_watchdog_timer)
            warnings = [a for a, kw in app.log_calls if kw.get("level") == "WARNING"]
            self.assertTrue(
                any("WATCHDOG" in str(a[0]) and "Paused" in str(a[0]) for a in warnings),
                f"expected a WATCHDOG warning naming Paused, got: {warnings}",
            )

    def test_watchdog_still_ignores_unemptied(self):
        """Guard against an overly broad fix: the watchdog must stay scoped to Running/Paused -
        Unemptied has its own, separate watchdog (_unemptied_watchdog_timeout) and must not be
        force-cleared by this one."""
        with tempfile.TemporaryDirectory() as tmp:
            app, entities = make_app(tmp, power_w="0")
            app.initialize()
            entities[STATE_ENTITY]["state"] = "Unemptied"
            app.state = "Unemptied"
            app.start_time = app._now_utc() - timedelta(hours=6)
            app.max_running_hours = 5

            app._running_watchdog_timeout({})

            self.assertEqual(app.state, "Unemptied")
            self.assertEqual(entities[STATE_ENTITY]["state"], "Unemptied")


if __name__ == "__main__":
    unittest.main()
