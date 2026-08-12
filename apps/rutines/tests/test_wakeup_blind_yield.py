"""The wake sequence yields to a manual press without stranding the morning.

wakeup_bedroom waits for the bedroom blind to REACH the wake target before it
starts the light ramp (cover listener + the wake_light_window_min expiry). The
gap the blind_arbiter seam deliberately left: a manual wall press mid-sequence
outranks the wake, the blind never reaches the target, and the listener used to
wait its full hour and then disarm WITHOUT a ramp - blind where the human put
it, but a dark, silent-lit morning.

With the owner:
  * the wake submits its blind request; a refusal (manual hold) is handled at
    the alarm - the wait completes immediately through _blind_wait_complete;
  * a manual takeover MID-wait is spotted from the position reports the takeover
    itself produces (the owner's manual hold is live from the first report), and
    the wait completes the same way - the ramp still runs;
  * the wake_light_window_min run_in stays armed as the bounded backstop, so no
    path can wait forever.

FAIL-SAFE: with the owner gone, every one of these paths collapses to the exact
pre-owner behaviour - direct write, listener, hour-long backstop. That is pinned
here too, because this is the alarm.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

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

import wakeup_bedroom as wb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds" / "tests"))
from owner_harness import make_owner, positions_commanded, report  # noqa: E402

COVER = "cover.bedroom_blind"
BATH = "cover.bathroom_blind"
ALARM = datetime(2026, 8, 12, 6, 0, 0, tzinfo=timezone.utc)


class FakeOwner:
    def __init__(self, granted=True, winner="wake", hold=False, boom=False):
        self.granted, self.winner, self.hold, self.boom = granted, winner, hold, boom
        self.requests = []

    def request(self, source, position, reason="", **kw):
        if self.boom:
            raise RuntimeError("owner exploded")
        self.requests.append((source, position, reason))
        return {"granted": self.granted, "winner": self.winner,
                "position": position if self.granted else None, "manual_until": None}

    def manual_hold_active(self):
        if self.boom:
            raise RuntimeError("owner exploded")
        return self.hold


def make_wake(owner=..., get_app_raises=False):
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.user_log = "test_log"
    app.log_lines = []
    app.log = lambda msg, **kw: app.log_lines.append(str(msg))
    app.call_service = MagicMock()
    app.bedroom_cover = COVER
    if owner is not ...:
        app.owner_app = "BedroomBlindOwner"
        if get_app_raises:
            def get_app(name):
                raise RuntimeError("registry down")
        else:
            def get_app(name):
                return owner if name == "BedroomBlindOwner" else None
        app.get_app = get_app
    return app


class SetCoverPositionRouting(unittest.TestCase):
    DIRECT = ("cover/set_cover_position", {"entity_id": COVER, "position": 38})

    def test_bedroom_write_goes_to_the_owner_with_the_decided_source(self):
        owner = FakeOwner(granted=True)
        app = make_wake(owner)
        app._current_bedroom_wake_source = "vent"
        app._current_bedroom_wake_reason = "venting tonight, window must open"
        app._set_cover_position(COVER, 38)
        self.assertEqual(owner.requests, [("vent", 38, "venting tonight, window must open")])
        app.call_service.assert_not_called()
        self.assertEqual(app._wake_blind_outcome, "granted")

    def test_refusal_is_recorded_not_retried(self):
        owner = FakeOwner(granted=False, winner="manual")
        app = make_wake(owner)
        app._set_cover_position(COVER, 38)
        self.assertEqual(app._wake_blind_outcome, "refused")
        app.call_service.assert_not_called()
        self.assertTrue(any("held by manual" in l for l in app.log_lines), app.log_lines)

    def test_owner_raising_degrades_to_the_exact_master_write(self):
        app = make_wake(FakeOwner(boom=True))
        app._set_cover_position(COVER, 38)
        self.assertEqual(
            (app.call_service.call_args.args[0], app.call_service.call_args.kwargs),
            self.DIRECT,
        )

    def test_get_app_raising_degrades_to_the_exact_master_write(self):
        app = make_wake(FakeOwner(), get_app_raises=True)
        app._set_cover_position(COVER, 38)
        self.assertEqual(
            (app.call_service.call_args.args[0], app.call_service.call_args.kwargs),
            self.DIRECT,
        )

    def test_half_loaded_owner_without_request_is_treated_as_absent(self):
        app = make_wake(types.SimpleNamespace())
        app._set_cover_position(COVER, 38)
        app.call_service.assert_called_once()

    def test_bathroom_is_never_routed_to_the_owner(self):
        owner = FakeOwner()
        app = make_wake(owner)
        app._set_cover_position(BATH, 40)
        self.assertEqual(owner.requests, [])
        app.call_service.assert_called_once_with(
            "cover/set_cover_position", entity_id=BATH, position=40
        )


def real_fire_app(owner, *, cover_pos=100):
    """Enough of WakeupRoutine for the REAL _alarm_fire's blind phase.

    _manual_fire_in_progress=True short-circuits the trigger-window and toggle
    guards (same trick as ManualFireBypassesToggleGuard) so only the blind wait
    is under test. Media/speaker/nudge phases are stubbed - their behaviour has
    its own files."""
    app = make_wake(owner)
    now = datetime(2026, 8, 12, 6, 0, 5)
    app.datetime = lambda: now
    app.get_state = lambda entity, **kw: {"input_datetime.wakeup_bedroom": "06:00:00"}.get(entity)
    app.alarm_time_entity = "input_datetime.wakeup_bedroom"
    app.alarm_enabled_entity = "input_boolean.wakeup_bedroom"
    app._manual_fire_in_progress = True
    app._late_catchup_in_progress = False
    app._fired_via_heartbeat = False
    app._state = {}
    app._save_state = lambda: None
    app._both_away = lambda: False
    app.fire_event = lambda *a, **kw: None
    app._attach_cancel_listeners = lambda: None
    app._group_speakers = lambda: None
    app._start_media_and_volume_ramp = lambda: None
    app.bathroom_cover = BATH
    app.bedroom_cover_target = 38
    app.bathroom_cover_target = 40
    app._decide_bedroom_wake_target = lambda: (38, "cool / no sun")
    app._nudge_cover_if_closed = lambda entity, target: (
        app._set_cover_position(entity, target) if entity == COVER else None
    )
    app._cover_position = lambda entity: cover_pos
    app._maybe_start_light_ramp = MagicMock()
    app.blind_wait_completions = []
    app._blind_wait_complete = lambda headline="": app.blind_wait_completions.append(headline)
    app.cover_listener = None
    app.listens = []
    app.listen_state = lambda cb, entity, **kw: app.listens.append((cb, entity, kw)) or "handle"
    app.cancel_listen_state = MagicMock()
    app.wake_light_window_min = 60
    app.scheduled = []
    app.run_in = lambda cb, delay, **kw: app.scheduled.append((cb, delay, kw)) or object()
    return app


class AlarmFireYieldsToAManualHold(unittest.TestCase):
    """Regression for the pre-existing-hold case: wall press shortly BEFORE the
    alarm -> the owner refuses the wake request -> no listener, no hour of
    waiting; the blind wait completes immediately and the ramp decision runs."""

    def test_refused_wake_request_completes_the_wait_immediately(self):
        owner = FakeOwner(granted=False, winner="manual", hold=True)
        app = real_fire_app(owner, cover_pos=100)
        app._alarm_fire({})
        self.assertEqual(app._wake_blind_outcome, "refused")
        self.assertEqual(len(app.blind_wait_completions), 1)
        self.assertEqual(app.listens, [])  # never armed the hour-long wait

    def test_hold_without_a_submitted_write_still_short_circuits(self):
        """Blind not closed at the alarm -> the nudge never submits -> outcome is
        None; the owner's hold alone must keep the wait from arming."""
        owner = FakeOwner(granted=True, hold=True)
        app = real_fire_app(owner, cover_pos=55)
        app._nudge_cover_if_closed = lambda entity, target: None  # not closed: no write
        app._alarm_fire({})
        self.assertEqual(len(app.blind_wait_completions), 1)
        self.assertEqual(app.listens, [])

    def test_no_owner_is_the_exact_master_path(self):
        """Owner deleted from disk: alarm fires, blind write goes out directly, the
        listener arms, and the bounded hour-long expiry is scheduled - byte-for-byte
        the pre-owner morning."""
        app = real_fire_app(..., cover_pos=100)  # make_wake(...) -> no owner wired
        app._alarm_fire({})
        app.call_service.assert_called_once_with(
            "cover/set_cover_position", entity_id=COVER, position=38
        )
        self.assertEqual(app.blind_wait_completions, [])
        self.assertEqual(len(app.listens), 1)
        expiries = [s for s in app.scheduled if s[0] == app._expire_cover_listener]
        self.assertEqual(len(expiries), 1)
        self.assertEqual(expiries[0][1], 60 * 60)  # the bounded backstop exists

    def test_granted_request_waits_for_the_target_as_always(self):
        owner = FakeOwner(granted=True, hold=False)
        app = real_fire_app(owner, cover_pos=100)
        app._alarm_fire({})
        self.assertEqual(app._wake_blind_outcome, "granted")
        self.assertEqual(app.blind_wait_completions, [])
        self.assertEqual(len(app.listens), 1)  # normal morning: wait for 38


class ManualPressTenSecondsAfterTheWakeRequest(unittest.TestCase):
    """THE regression the owner exists for, end-to-end with the REAL owner app:

    alarm submits wake(38) -> blind starts opening -> a wall press 10 s later
    ranks manual(100) -> the blind stays where the human sent it (the wake never
    re-commands) AND the light ramp still starts."""

    def _wire(self):
        owner = make_owner(position=100, now=ALARM)
        app = make_wake(owner)
        app._current_bedroom_wake_target = 38
        app._current_bedroom_wake_source = "wake"
        app._current_bedroom_wake_reason = "cool / no sun"
        app.cover_listener = "listener-handle"
        app.cancel_listen_state = MagicMock()
        app.fire_event = lambda *a, **kw: None
        app._room_dark_for_wake_light = lambda: True
        app._maybe_start_light_ramp = MagicMock()
        app._compute_post_blind_settle_delay = lambda: (10, 4000.0)
        app.scheduled = []
        app.run_in = lambda cb, delay, **kw: app.scheduled.append((cb, delay, kw)) or object()
        return owner, app

    def _feed(self, owner, app, pos):
        """One position report reaches both listeners, as it does in production."""
        report(owner, pos)
        app._on_bedroom_position(COVER, "current_position", None, str(pos), {})

    def test_blind_stays_with_the_human_and_the_ramp_still_starts(self):
        owner, app = self._wire()

        # 06:00:00 - the wake submits and the owner commands the open.
        app._set_cover_position(COVER, 38)
        self.assertEqual(app._wake_blind_outcome, "granted")
        self.assertEqual(positions_commanded(owner), [38])

        # Travel begins; nothing manual yet, so the wait keeps waiting.
        self._feed(owner, app, 90)
        self.assertEqual(app.scheduled, [])

        # 06:00:10 - wall press: bedroom_blind_control ranks it manual.
        res = owner.request("manual", 100, reason="wall press: close hold")
        self.assertTrue(res["granted"])
        self.assertEqual(positions_commanded(owner), [38, 100])
        self.assertTrue(owner.manual_hold_active())

        # The takeover's own position reports trigger the yield.
        self._feed(owner, app, 95)
        app.cancel_listen_state.assert_called_once_with("listener-handle")
        self.assertIsNone(app.cover_listener)
        decides = app.scheduled
        self.assertEqual(len(decides), 1)
        self.assertEqual(decides[0][1], 10)  # post_blind_settle timing, unchanged

        # The settle elapses -> the decide runs -> the ramp STILL starts.
        decides[0][0](None)
        app._maybe_start_light_ramp.assert_called_once()

        # And the blind stayed where the human put it: the wake never re-commanded.
        self.assertEqual(positions_commanded(owner), [38, 100])
        app.call_service.assert_not_called()  # wakeup itself never wrote the cover

    def test_further_reports_do_not_double_complete_the_wait(self):
        owner, app = self._wire()
        app._set_cover_position(COVER, 38)
        owner.request("manual", 100, reason="wall press")
        self._feed(owner, app, 95)
        self._feed(owner, app, 100)  # travel finishes reporting after the yield
        app.cancel_listen_state.assert_called_once()
        self.assertEqual(len(app.scheduled), 1)


if __name__ == "__main__":
    unittest.main()
